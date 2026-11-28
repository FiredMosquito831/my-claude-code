"""Safe probe, plan, apply, and undo for permanent user-config MCP entries.

Supports JSON/JSONC, TOML, and YAML user config files with:
- Unified diff previews before any write
- One-time ``.mcc-backup`` before MCC's first edit
- Byte-preserving edits and splice-based undo (``keys_only`` and ``restore``)
- Foreign/user-authored ``mcc_media`` protection (never overwritten or deleted)
- Shared target reconciliation (e.g. ``codex`` <-> ``codex_desktop``)

``config`` is a leaf package: stdlib and ``config`` only.
"""

import copy
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import unified_diff
from enum import StrEnum
from pathlib import Path
from typing import cast

from my_claude_code.config.desktop_apply import managed_override
from my_claude_code.config.desktop_apps import DESKTOP_APPS_BY_ID, DesktopAppSpec
from my_claude_code.config.document_codecs import (
    DeleteKey,
    DocumentFormat,
    DocumentFormatError,
    SetScalar,
    SetTable,
    apply_edits,
    mask_json_text,
    parse_document,
    yaml_mapping_at,
    yaml_root_is_sequence,
)
from my_claude_code.config.mcp_media import (
    MCP_MEDIA_SERVER_NAME,
    MCP_PERMANENT_TARGETS,
    PermanentMcpTargetSpec,
    desktop_file_entry,
    mcp_media_command,
    opencode_permission_keys,
    permanent_mcp_target,
    resolve_permanent_mcp_path,
)
from my_claude_code.config.paths import mcp_media_output_dir
from my_claude_code.config.restore_record import (
    McpServerRecord,
    UndoMode,
    document_sha256,
    forget_mcp_record,
    read_mcp_record,
    refresh_document_sha256,
    value_at,
    write_mcp_record,
)

_SECRET_MASK_KEYS: frozenset[str] = frozenset(
    {
        "apikey",
        "api_key",
        "auth_token",
        "authtoken",
        "token",
        "secret",
        "password",
        "bearer",
    }
)


class PermanentMcpError(RuntimeError):
    """Refusal with an actionable reason for permanent MCP plan or apply."""


class PermanentMcpState(StrEnum):
    """Live state of one permanent MCP config target."""

    UNSUPPORTED = "unsupported"
    ABSENT = "absent"
    CONFIGURED = "configured"
    DRIFTED = "drifted"
    FOREIGN = "foreign"
    UNREADABLE = "unreadable"
    MANAGED = "managed"


@dataclass(frozen=True, slots=True)
class PermanentMcpProbe:
    """What one permanent MCP target looks like on disk right now."""

    target_id: str
    state: PermanentMcpState
    document_path: str
    document_exists: bool
    key_label: str
    backup_path: str = ""
    backup_exists: bool = False
    restorable: bool = False
    managed_by: str = ""
    error: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PermanentMcpPlan:
    """Preview of adding or removing a permanent MCP entry, touching no disk."""

    target_id: str
    add: bool
    mode: UndoMode
    document_path: str
    backup_path: str
    diff: str
    will_create_file: bool
    removes_file: bool
    backup_exists: bool
    overwrites: tuple[str, ...]
    restorable: bool
    no_op: bool
    ask_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PermanentMcpResult:
    """Outcome of applying or removing a permanent MCP entry."""

    target_id: str
    changed: bool
    added: bool
    mode: UndoMode
    document_path: str
    backup_path: str = ""
    created_file: bool = False
    removed_file: bool = False


@dataclass(frozen=True, slots=True)
class _PermanentChange:
    path: Path
    before: str
    after: str
    crlf: bool
    existed: bool
    no_op: bool
    delete_file: bool = False
    overwrites: tuple[str, ...] = ()
    record: McpServerRecord | None = None
    existing_record: McpServerRecord | None = None


def _as_object_map(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _set_in(
    document: dict[str, object], key_path: Sequence[str], value: object
) -> None:
    cursor: dict[str, object] = document
    for key in key_path[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            child = {}
            cursor[key] = child
        cursor = cast(dict[str, object], child)
    cursor[key_path[-1]] = value


def _delete_in(document: dict[str, object], key_path: Sequence[str]) -> None:
    cursor: object = document
    for key in key_path[:-1]:
        if not isinstance(cursor, dict) or key not in cursor:
            return
        cursor = cursor.get(key)
    if isinstance(cursor, dict):
        cursor.pop(key_path[-1], None)


def _read_text(path: Path) -> tuple[str | None, bool]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, False
    except OSError as exc:
        raise PermanentMcpError(f"cannot read {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PermanentMcpError(f"{path} is not UTF-8 text: {exc}") from exc
    crlf = "\r\n" in text and "\n" not in text.replace("\r\n", "")
    return text.replace("\r\n", "\n").replace("\r", "\n"), crlf


def _parse_mcp_doc(text: str, document_format: DocumentFormat) -> dict[str, object]:
    if not text.strip():
        return {}
    if document_format is DocumentFormat.YAML:
        if yaml_root_is_sequence(text):
            raise DocumentFormatError("YAML root is a sequence, expected a mapping")
        mapping = yaml_mapping_at(text, ())
        if mapping is None:
            raise DocumentFormatError("the YAML document is not a mapping")
        return mapping
    mapping = _as_object_map(parse_document(text, document_format))
    if mapping is None:
        raise DocumentFormatError("the document is not a map of keys")
    return mapping


def _backup_path_for(path: Path, spec: PermanentMcpTargetSpec) -> Path:
    return path.with_name(path.name + spec.backup_suffix)


def _backup_once(path: Path, suffix: str) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(path.name + suffix)
    if backup.exists():
        return backup
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, backup)
    if sys.platform != "win32":
        shutil.copymode(path, backup)
    return backup


def _desktop_spec_for(spec: PermanentMcpTargetSpec) -> DesktopAppSpec | None:
    if not spec.desktop_app_id:
        return None
    return DESKTOP_APPS_BY_ID.get(spec.desktop_app_id)


def _check_managed(spec: PermanentMcpTargetSpec, env: Mapping[str, str]) -> str:
    desktop_spec = _desktop_spec_for(spec)
    if desktop_spec is None:
        return ""
    managed_by, keys = managed_override(desktop_spec, env)
    if managed_by:
        key_text = ", ".join(keys)
        return (
            f"{spec.display_name} is configured by {managed_by}, which outranks "
            f"the files MCC writes ({key_text}). MCC will not write a file the "
            "app is going to ignore."
        )
    return ""


def _owned_by_record(
    record: McpServerRecord | None,
    path: Path,
    document: Mapping[str, object],
    spec: PermanentMcpTargetSpec,
) -> bool:
    if record is None or record.document_path != str(path):
        return False
    value, present = value_at(document, spec.entry_key_path)
    return present and value == record.server_value


def _foreign_message(spec: PermanentMcpTargetSpec, path: Path) -> str:
    return (
        f"{path} already has an MCP server named {MCP_MEDIA_SERVER_NAME} that "
        "MCC did not add, or that has been changed since MCC added it. It is "
        "yours, so MCC leaves it exactly as it is: Remove or overwrite is "
        f"refused. Rename or remove it there if you want {spec.display_name} "
        "to manage MCC's own."
    )


def _splice(before: str, after: str) -> tuple[str, str]:
    limit = min(len(before), len(after))
    prefix = 0
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]
    ):
        suffix += 1
    return after[prefix : len(after) - suffix], before[prefix : len(before) - suffix]


def _set_edit(key_path: tuple[str, ...], value: object) -> SetTable | SetScalar:
    if isinstance(value, Mapping):
        return SetTable(key_path, {str(key): item for key, item in value.items()})
    return SetScalar(key_path, value)


def _created_keys(
    document: Mapping[str, object],
    spec: PermanentMcpTargetSpec,
    entry: Mapping[str, object],
) -> list[tuple[tuple[str, ...], object]]:
    created: list[tuple[tuple[str, ...], object]] = [
        (spec.entry_key_path, copy.deepcopy(dict(entry)))
    ]
    if spec.ask_map_key:
        current, present = value_at(document, spec.ask_map_key)
        mapping: Mapping[str, object] | None = (
            _as_object_map(current) if present else {}
        )
        if mapping is not None:
            created.extend(
                ((*spec.ask_map_key, key), "ask")
                for key in opencode_permission_keys()
                if key not in mapping
            )
    return created


def _absent_parents(
    document: Mapping[str, object],
    created: Sequence[tuple[tuple[str, ...], object]],
) -> tuple[tuple[str, ...], ...]:
    parents: list[tuple[str, ...]] = []
    for key_path, _value in created:
        for depth in range(1, len(key_path)):
            prefix = key_path[:depth]
            _found, present = value_at(document, prefix)
            if not present and prefix not in parents:
                parents.append(prefix)
    return tuple(parents)


def _add_text(
    before: str,
    document_format: DocumentFormat,
    created: Sequence[tuple[tuple[str, ...], object]],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    if not before.strip():
        if document_format in (DocumentFormat.TOML, DocumentFormat.YAML):
            text = ""
            for key_path, value in created:
                text = apply_edits(text, document_format, [_set_edit(key_path, value)])
        else:
            built: dict[str, object] = {}
            for key_path, value in created:
                _set_in(built, key_path, copy.deepcopy(value))
            text = json.dumps(built, indent=2) + "\n"
        return text, ((text, before),)
    text = before
    splices: list[tuple[str, str]] = []
    for key_path, value in created:
        updated = apply_edits(text, document_format, [_set_edit(key_path, value)])
        splices.append(_splice(text, updated))
        text = updated
    return text, tuple(splices)


def _removal_text(
    text: str,
    document_format: DocumentFormat,
    record: McpServerRecord,
    *,
    exact_only: bool = False,
) -> str | None:
    try:
        document = _parse_mcp_doc(text, document_format)
    except DocumentFormatError:
        return None
    owned = [
        (key_path, value)
        for key_path, value in record.created
        if value_at(document, key_path) == (value, True)
    ]
    if not owned or owned[0][0] != record.created[0][0]:
        return None
    expected = copy.deepcopy(document)
    for key_path, _value in owned:
        _delete_in(expected, key_path)
    for parent in sorted(record.created_parents, key=len, reverse=True):
        value, present = value_at(expected, parent)
        if present and value == {}:
            _delete_in(expected, parent)

    def matches(candidate: str) -> bool:
        try:
            return _parse_mcp_doc(candidate, document_format) == expected
        except DocumentFormatError:
            return False

    if len(owned) == len(record.created) and record.splices:
        candidate: str | None = text
        for inserted, replaced in reversed(record.splices):
            if candidate is None or not inserted or candidate.count(inserted) != 1:
                candidate = None
                break
            candidate = candidate.replace(inserted, replaced, 1)
        if candidate is not None and matches(candidate):
            return candidate
    if exact_only:
        return None
    edits: list[SetTable | SetScalar | DeleteKey] = [
        DeleteKey(
            key_path,
            prune_empty_ancestors=key_path[:-1] in record.created_parents,
        )
        for key_path, _value in reversed(owned)
    ]
    try:
        candidate = apply_edits(text, document_format, edits)
    except DocumentFormatError:
        return None
    return candidate if matches(candidate) else None


def _is_empty_doc(text: str) -> bool:
    return text.strip() in ("", "{}")


def _mask_for_diff(text: str, document_format: DocumentFormat) -> str:
    if document_format is DocumentFormat.JSON:
        return mask_json_text(text, _SECRET_MASK_KEYS)
    return text


def _diff_text(before: str, after: str, path: Path) -> str:
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def permanent_entry_for(
    spec: PermanentMcpTargetSpec,
    *,
    env: Mapping[str, str] | None = None,
    command: str | None = None,
) -> dict[str, object] | None:
    """Build the expected ``mcc_media`` entry dict for ``spec``."""

    if not spec.permanent_supported or spec.shape is None:
        return None
    resolved = command if command is not None else mcp_media_command()
    if not resolved:
        return None
    source_env = os.environ if env is None else env
    out_dir = str(mcp_media_output_dir()) if spec.needs_out_dir else None
    return desktop_file_entry(
        spec.shape,
        spec.effective_entry_app_id,
        resolved,
        source_env,
        out_dir=out_dir,
    )


def _require_supported(target_id: str) -> PermanentMcpTargetSpec:
    spec = permanent_mcp_target(target_id)
    if spec is None:
        known = ", ".join(item.target_id for item in MCP_PERMANENT_TARGETS)
        raise KeyError(f"unknown permanent MCP target: {target_id}. Known: {known}")
    if not spec.permanent_supported or spec.document_format is None:
        raise PermanentMcpError(
            spec.unsupported_reason
            or f"MCC does not support permanent MCP config for {spec.display_name}."
        )
    return spec


def _compute_change(
    spec: PermanentMcpTargetSpec,
    *,
    env: Mapping[str, str],
    add: bool,
    mode: UndoMode,
    entry: Mapping[str, object] | None,
    record_path: Path | None,
    platform: str | None,
) -> _PermanentChange:
    assert spec.document_format is not None
    managed_msg = _check_managed(spec, env)
    if managed_msg:
        raise PermanentMcpError(managed_msg)
    path = resolve_permanent_mcp_path(spec, env, platform=platform)
    if path is None:
        raise PermanentMcpError(
            f"{spec.display_name} declares no MCP config path for this platform."
        )
    raw_text, crlf = _read_text(path)
    existed = raw_text is not None
    before = raw_text or ""
    try:
        document = _parse_mcp_doc(before, spec.document_format)
    except DocumentFormatError as exc:
        raise PermanentMcpError(
            f"cannot parse {path}: {exc}. MCC writes nothing into a file it cannot read."
        ) from exc

    record = read_mcp_record(spec.effective_record_subject, path=record_path)
    value, present = value_at(document, spec.entry_key_path)
    owned = _owned_by_record(record, path, document, spec)
    if present and not owned:
        raise PermanentMcpError(_foreign_message(spec, path))

    if not add:
        if not present or record is None:
            return _PermanentChange(path, before, before, crlf, existed, no_op=True)
        after = _removal_text(
            before,
            spec.document_format,
            record,
            exact_only=mode is UndoMode.RESTORE,
        )
        if after is None:
            if mode is UndoMode.RESTORE:
                raise PermanentMcpError(
                    f"{path} changed since MCC wrote its MCP entry, so exact "
                    "restore cannot proceed without reverting later edits. Use "
                    "keys_only mode or remove mcc_media by hand."
                )
            raise PermanentMcpError(
                f"MCC could not remove {spec.entry_key_label} from {path} "
                "without changing anything else in it, so it wrote nothing."
            )
        return _PermanentChange(
            path,
            before,
            after,
            crlf,
            existed,
            no_op=after == before,
            delete_file=record.created_file and _is_empty_doc(after),
            record=record,
            existing_record=record,
        )

    if entry is None:
        raise PermanentMcpError(
            "mcc-mcp is not installed beside this MCC, so there is nothing to add."
        )
    if present and value == dict(entry):
        return _PermanentChange(
            path,
            before,
            before,
            crlf,
            existed,
            no_op=True,
            existing_record=record,
        )

    base = before
    created_file = not existed
    created_dirs: tuple[str, ...] = ()
    overwrites: tuple[str, ...] = ()
    if present and record is not None:
        overwrites = (spec.entry_key_label,)
        removed = _removal_text(before, spec.document_format, record)
        if removed is None:
            raise PermanentMcpError(
                f"MCC could not update {spec.entry_key_label} in {path} "
                "without changing anything else in it, so it wrote nothing."
            )
        base = removed
        created_file = record.created_file
        created_dirs = record.created_dirs

    base_document = _parse_mcp_doc(base, spec.document_format)
    created = _created_keys(base_document, spec, entry)
    try:
        after, splices = _add_text(base, spec.document_format, created)
    except DocumentFormatError as exc:
        raise PermanentMcpError(f"cannot add MCC's entry to {path}: {exc}") from exc

    expected = copy.deepcopy(base_document)
    for key_path, item in created:
        _set_in(expected, key_path, copy.deepcopy(item))
    try:
        produced = _parse_mcp_doc(after, spec.document_format)
    except DocumentFormatError:
        produced = None
    if produced != expected:
        raise PermanentMcpError(
            f"MCC could not add {spec.entry_key_label} to {path} without "
            "changing something else in it, so it wrote nothing."
        )
    if not existed:
        missing: list[str] = []
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            missing.append(str(parent))
            parent = parent.parent
        created_dirs = tuple(missing)

    return _PermanentChange(
        path,
        before,
        after,
        crlf,
        existed,
        no_op=after == before,
        overwrites=overwrites,
        record=McpServerRecord(
            subject=spec.effective_record_subject,
            document_path=str(path),
            created=tuple(created),
            splices=splices,
            created_parents=_absent_parents(base_document, created),
            created_file=created_file,
            created_dirs=created_dirs,
        ),
        existing_record=record,
    )


def probe_permanent_mcp(
    spec: PermanentMcpTargetSpec,
    *,
    env: Mapping[str, str] | None = None,
    expected_entry: Mapping[str, object] | None = None,
    record_path: Path | None = None,
    platform: str | None = None,
) -> PermanentMcpProbe:
    """Inspect one permanent MCP target on disk without mutating anything."""

    if not spec.permanent_supported or spec.document_format is None:
        return PermanentMcpProbe(
            target_id=spec.target_id,
            state=PermanentMcpState.UNSUPPORTED,
            document_path="",
            document_exists=False,
            key_label="",
            reason=spec.unsupported_reason,
        )
    source_env = os.environ if env is None else env
    label = spec.entry_key_label
    path = resolve_permanent_mcp_path(spec, source_env, platform=platform)
    display = str(path) if path is not None else ""

    desktop_spec = _desktop_spec_for(spec)
    if desktop_spec is not None:
        managed_by, _keys = managed_override(desktop_spec, source_env)
        if managed_by:
            return PermanentMcpProbe(
                target_id=spec.target_id,
                state=PermanentMcpState.MANAGED,
                document_path=display,
                document_exists=bool(path and path.exists()),
                key_label=label,
                managed_by=managed_by,
                reason=f"Outranked by {managed_by}.",
            )

    if path is None:
        return PermanentMcpProbe(
            target_id=spec.target_id,
            state=PermanentMcpState.UNREADABLE,
            document_path="",
            document_exists=False,
            key_label=label,
            error="No MCP config path resolved for this platform.",
        )

    backup = _backup_path_for(path, spec)
    backup_exists = backup.exists()
    backup_display = str(backup) if backup_exists else ""
    try:
        text, _crlf = _read_text(path)
        document = _parse_mcp_doc(text or "", spec.document_format)
    except (PermanentMcpError, DocumentFormatError) as exc:
        return PermanentMcpProbe(
            target_id=spec.target_id,
            state=PermanentMcpState.UNREADABLE,
            document_path=display,
            document_exists=path.exists(),
            key_label=label,
            backup_path=backup_display,
            backup_exists=backup_exists,
            error=str(exc),
            reason=str(exc),
        )

    record = read_mcp_record(spec.effective_record_subject, path=record_path)
    value, present = value_at(document, spec.entry_key_path)
    if not present:
        state = PermanentMcpState.ABSENT
        restorable = False
    elif not _owned_by_record(record, path, document, spec):
        state = PermanentMcpState.FOREIGN
        restorable = False
    elif expected_entry is not None and value != dict(expected_entry):
        state = PermanentMcpState.DRIFTED
        restorable = bool(record and record.splices)
    else:
        state = PermanentMcpState.CONFIGURED
        restorable = bool(record and record.splices)

    return PermanentMcpProbe(
        target_id=spec.target_id,
        state=state,
        document_path=display,
        document_exists=text is not None,
        key_label=label,
        backup_path=backup_display,
        backup_exists=backup_exists,
        restorable=restorable,
    )


def plan_permanent_mcp(
    target_id: str,
    *,
    add: bool,
    mode: UndoMode = UndoMode.KEYS_ONLY,
    env: Mapping[str, str] | None = None,
    entry: Mapping[str, object] | None = None,
    record_path: Path | None = None,
    platform: str | None = None,
) -> PermanentMcpPlan:
    """Preview adding or removing a permanent MCP entry, touching no disk."""

    spec = _require_supported(target_id)
    assert spec.document_format is not None
    source_env = os.environ if env is None else env
    resolved_entry = (
        entry
        if entry is not None or not add
        else permanent_entry_for(spec, env=source_env)
    )
    change = _compute_change(
        spec,
        env=source_env,
        add=add,
        mode=mode,
        entry=resolved_entry,
        record_path=record_path,
        platform=platform,
    )
    backup = _backup_path_for(change.path, spec)
    after = "" if change.delete_file else change.after
    diff = (
        ""
        if change.no_op
        else _diff_text(
            _mask_for_diff(change.before, spec.document_format),
            _mask_for_diff(after, spec.document_format),
            change.path,
        )
    )
    ask_keys = (
        tuple(".".join(key_path) for key_path, _val in change.record.created[1:])
        if add and change.record is not None and not change.no_op
        else ()
    )
    restorable = bool(
        change.existing_record is not None
        and change.existing_record.splices
        and (
            backup.exists()
            or change.existing_record.created_file
            or bool(change.existing_record.splices)
        )
    )
    return PermanentMcpPlan(
        target_id=spec.target_id,
        add=add,
        mode=mode,
        document_path=str(change.path),
        backup_path=str(backup),
        diff=diff,
        will_create_file=add and not change.existed and not change.no_op,
        removes_file=change.delete_file,
        backup_exists=backup.exists(),
        overwrites=change.overwrites,
        restorable=restorable,
        no_op=change.no_op,
        ask_keys=ask_keys,
    )


def _write_text_atomically(path: Path, text: str, *, crlf: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".mcc-tmp")
    data = text.replace("\n", "\r\n") if crlf else text
    temporary.write_bytes(data.encode("utf-8"))
    if path.exists() and sys.platform != "win32":
        shutil.copymode(path, temporary)
    os.replace(temporary, path)


def _remove_created(path: Path, record: McpServerRecord) -> None:
    path.unlink(missing_ok=True)
    for directory in record.created_dirs:
        try:
            os.rmdir(directory)
        except OSError:
            break


def _same_desktop_document(
    spec: PermanentMcpTargetSpec, env: Mapping[str, str], path: Path
) -> bool:
    desktop_spec = _desktop_spec_for(spec)
    if desktop_spec is None or desktop_spec.document is None:
        return False
    from my_claude_code.config.desktop_apply import document_path_for

    return document_path_for(desktop_spec, env) == path


def apply_permanent_mcp(
    target_id: str,
    *,
    add: bool,
    mode: UndoMode = UndoMode.KEYS_ONLY,
    env: Mapping[str, str] | None = None,
    entry: Mapping[str, object] | None = None,
    record_path: Path | None = None,
    platform: str | None = None,
) -> PermanentMcpResult:
    """Write or remove one permanent MCP entry atomically with backup + record."""

    spec = _require_supported(target_id)
    source_env = os.environ if env is None else env
    resolved_entry = (
        entry
        if entry is not None or not add
        else permanent_entry_for(spec, env=source_env)
    )
    change = _compute_change(
        spec,
        env=source_env,
        add=add,
        mode=mode,
        entry=resolved_entry,
        record_path=record_path,
        platform=platform,
    )
    path = change.path
    backup = _backup_path_for(path, spec)
    subject = spec.effective_record_subject
    if change.no_op:
        if not add:
            forget_mcp_record(subject, path=record_path)
        return PermanentMcpResult(
            target_id=spec.target_id,
            changed=False,
            added=add,
            mode=mode,
            document_path=str(path),
            backup_path=str(backup) if backup.exists() else "",
        )

    before_sha = document_sha256(path) if change.existed else ""
    if change.delete_file and change.record is not None:
        _remove_created(path, change.record)
    else:
        if change.existed and change.before.strip():
            _backup_once(path, spec.backup_suffix)
        _write_text_atomically(path, change.after, crlf=change.crlf)

    if before_sha and path.exists() and _same_desktop_document(spec, source_env, path):
        refresh_document_sha256(
            subject,
            before=before_sha,
            after=document_sha256(path),
            path=record_path,
        )

    if add and change.record is not None:
        write_mcp_record(change.record, path=record_path)
    elif not add:
        forget_mcp_record(subject, path=record_path)

    return PermanentMcpResult(
        target_id=spec.target_id,
        changed=True,
        added=add,
        mode=mode,
        document_path=str(path),
        backup_path=str(backup) if backup.exists() else "",
        created_file=add and not change.existed,
        removed_file=change.delete_file,
    )


__all__ = [
    "PermanentMcpError",
    "PermanentMcpPlan",
    "PermanentMcpProbe",
    "PermanentMcpResult",
    "PermanentMcpState",
    "apply_permanent_mcp",
    "permanent_entry_for",
    "plan_permanent_mcp",
    "probe_permanent_mcp",
]
