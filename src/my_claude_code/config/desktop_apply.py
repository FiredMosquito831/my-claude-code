"""Probe, plan, apply and undo, for one desktop app's configuration document.

The mechanism half of the desktop-apps feature. It reads
:mod:`my_claude_code.config.desktop_apps` for *what* a given app declares and
does the same four things for every one of them:

``probe``   what state is this app in right now
``plan``    what would Configure write, rendered as a diff, touching no disk
``apply``   write it, backing up once and recording what was overwritten
``undo``    take it back out, in one of two modes

Plan is separate from apply and never touches disk, which is the doctrine
``config/claude_config_editor.py`` already sets: the page shows a real diff of
a real document before anything is written, and apply re-plans server-side
rather than trusting the diff the browser hands back.

**The block is an argument, not something built here.** What MCC writes into an
app's document is a model catalogue, and catalogues live in ``application/``,
which ``config/`` may not import -- it is a leaf, by contract. So the caller
resolves the block and passes it in. That is not a workaround; it is what keeps
this module a pure mechanism that a test can drive with a hand-written dict.

**The two undo modes.** ``KEYS_ONLY`` removes what MCC owns and leaves every
other byte alone -- the mode that cannot surprise anyone, and the default.
``RESTORE`` additionally puts back the values MCC replaced, read from the side
record in ``config/restore_record.py``. Both are offered because both are
right answers to different questions: a user who had ``model`` set to their own
choice wants it back, and a user who never had one wants the key gone rather
than resurrected as ``null``.
"""

import json
import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from difflib import unified_diff
from pathlib import Path

from my_claude_code.config.desktop_apps import (
    DesktopAppSpec,
    DesktopAppState,
    DesktopAppStatus,
    DesktopDocument,
    DesktopPath,
    DesktopSidecar,
)
from my_claude_code.config.document_codecs import (
    DeleteKey,
    DocumentFormat,
    DocumentFormatError,
    SetScalar,
    SetTable,
    apply_edits,
    parse_document,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.restore_record import (
    RestoreEntry,
    UndoMode,
    capture_overwritten,
    document_sha256,
    forget_entry,
    read_entry,
    write_entry,
)


class DesktopApplyError(RuntimeError):
    """A document could not be read, or the app declares nothing to write."""


@dataclass(frozen=True, slots=True)
class DesktopProbe:
    """What one app looks like on this machine right now."""

    app_id: str
    state: DesktopAppState
    #: The document MCC would edit, absolute, or "" for a card with none.
    document_path: str
    #: Whether that document exists.
    document_exists: bool
    #: The parse error, when the state is ``unreadable``.
    error: str = ""
    #: Whether the environment variable the app needs is exported *here*.
    #: MCC never sets it; this is the "and verify on the next poll" half of
    #: the decision not to write a user-scope variable behind anyone's back.
    token_env_present: bool = False
    #: Whether a restore record exists, i.e. whether Undo can offer RESTORE.
    restorable: bool = False


@dataclass(slots=True)
class DesktopPlan:
    """What Configure would do, with nothing written."""

    app_id: str
    document_path: str
    #: Unified diff of the document, secrets already masked.
    diff: str
    #: The sidecar file MCC would own outright, when the app has one.
    sidecar_path: str = ""
    sidecar_diff: str = ""
    #: Keys that would be replaced rather than created, for the card to name.
    overwritten_keys: tuple[str, ...] = ()
    #: True when the document is already exactly what a re-apply would write.
    no_op: bool = False
    #: Lines the card shows verbatim: exports to run, restarts to perform.
    actions: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DesktopWriteResult:
    """What one apply or undo actually did."""

    app_id: str
    changed: bool
    document_path: str
    backup_path: str = ""
    sidecar_path: str = ""
    removed_sidecar: bool = False
    restored_keys: tuple[str, ...] = ()


#: What a token looks like once it is safe to show. The plan diff is rendered
#: into a browser and into a terminal, and a document MCC is about to write
#: can carry a literal credential in exactly one place -- the MCC-owned
#: sidecar for an app that resolves no reference form. Masking is applied to
#: the rendering, never to the bytes.
MASK = "***"

#: Field names whose values are masked in any rendered diff, matched
#: case-insensitively against the whole key. Deliberately a denylist of key
#: names rather than a scan for token-shaped strings: a scan cannot tell a
#: credential from a model id, and a missed mask is a leaked key.
_SECRET_KEYS: frozenset[str] = frozenset(
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


def _platform() -> str:
    return sys.platform


def resolve_path(paths: Sequence[DesktopPath], env: Mapping[str, str]) -> Path | None:
    """Return the first declared path that applies to this platform.

    Resolution follows the *app's* variable order, not Python's. A path whose
    ``env_vars`` are all unset falls back to the home directory, which is why
    the last entry in each app's tuple is always the plain home form.
    """

    platform = _platform()
    for candidate in paths:
        if candidate.platforms and platform not in candidate.platforms:
            continue
        if candidate.in_mcc_config_dir:
            return config_dir_path().joinpath(*candidate.relative_parts).absolute()
        for name in candidate.env_vars:
            value = env.get(name, "").strip()
            if value:
                return Path(value).joinpath(*candidate.relative_parts).absolute()
        if not candidate.env_vars:
            return Path.home().joinpath(*candidate.relative_parts).absolute()
    for candidate in paths:
        if candidate.platforms and platform not in candidate.platforms:
            continue
        return Path.home().joinpath(*candidate.relative_parts).absolute()
    return None


def document_path_for(spec: DesktopAppSpec, env: Mapping[str, str]) -> Path | None:
    """Return the document MCC would edit for one app, or None when it has none."""

    if spec.document is None:
        return None
    return resolve_path(spec.document.paths, env)


def sidecar_path_for(spec: DesktopAppSpec, env: Mapping[str, str]) -> Path | None:
    """Return the file MCC owns outright for one app, or None."""

    if spec.sidecar is None:
        return None
    return resolve_path(spec.sidecar.paths, env)


def is_installed(spec: DesktopAppSpec, env: Mapping[str, str]) -> bool:
    """Return whether any of the app's marker paths exists.

    A marker is a directory the app creates on first run, never its config
    file: an app that has run but has never been configured has to read as
    installed, or Configure would refuse the one case it exists for.
    """

    if spec.detect is None:
        return False
    platform = _platform()
    for marker in spec.detect.markers:
        if marker.platforms and platform not in marker.platforms:
            continue
        resolved = resolve_path((marker,), env)
        if resolved is not None and resolved.exists():
            return True
    return False


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", newline=None)
    except OSError:
        return None


def _load(path: Path, document_format: DocumentFormat) -> tuple[object | None, str]:
    """Return ``(document, error)``. A missing file is an empty document."""

    from my_claude_code.config.document_codecs import empty_document

    text = _read_text(path)
    if text is None:
        return empty_document(document_format), ""
    if not text.strip():
        return empty_document(document_format), ""
    try:
        return parse_document(text, document_format), ""
    except DocumentFormatError as exc:
        return None, str(exc)


def owned_value(document: object, document_spec: DesktopDocument) -> object | None:
    """Return what MCC owns inside a document as it is on disk, or None."""

    if document_spec.document_format is DocumentFormat.JSON_ARRAY:
        if not isinstance(document, list):
            return None
        for element in document:
            if (
                isinstance(element, dict)
                and element.get(document_spec.match_field) == document_spec.match_value
            ):
                return element
        return None

    if not document_spec.owned_key_path:
        # An app whose whole footprint is its overwritten scalars -- Goose and
        # Antigravity. Ownership is the scalars themselves.
        return None

    node: object = document
    for key in document_spec.owned_key_path:
        mapping = _as_object_map(node)
        if mapping is None or key not in mapping:
            return None
        node = mapping[key]
    return node


def probe(
    spec: DesktopAppSpec,
    *,
    env: Mapping[str, str],
    expected_block: Mapping[str, object] | None = None,
    expected_scalars: Mapping[str, object] | None = None,
    record_path: Path | None = None,
) -> DesktopProbe:
    """Return the state of one app, which is exactly the badge its card shows.

    ``expected_block`` is what a re-apply would write. It is optional because
    the CLI's ``status`` runs without a server and cannot resolve a catalogue;
    without it the probe distinguishes present from absent but never claims
    ``drifted``, which would be a guess.
    """

    if spec.status is DesktopAppStatus.NOT_ROUTABLE:
        return DesktopProbe(
            app_id=spec.id,
            state=DesktopAppState.NOT_ROUTABLE,
            document_path="",
            document_exists=False,
        )

    installed = is_installed(spec, env)
    token_env_present = bool(spec.token_env_var and env.get(spec.token_env_var, ""))
    restorable = read_entry(spec.id, path=record_path) is not None

    if spec.document is None:
        return DesktopProbe(
            app_id=spec.id,
            state=(
                DesktopAppState.INSTALLED
                if installed
                else DesktopAppState.NOT_INSTALLED
            ),
            document_path="",
            document_exists=False,
            token_env_present=token_env_present,
        )

    path = document_path_for(spec, env)
    if path is None:
        raise DesktopApplyError(f"{spec.id} declares no path for this platform")

    exists = path.exists()
    if not installed:
        return DesktopProbe(
            app_id=spec.id,
            state=DesktopAppState.NOT_INSTALLED,
            document_path=str(path),
            document_exists=exists,
            token_env_present=token_env_present,
            restorable=restorable,
        )

    document, error = _load(path, spec.document.document_format)
    if document is None:
        return DesktopProbe(
            app_id=spec.id,
            state=DesktopAppState.UNREADABLE,
            document_path=str(path),
            document_exists=exists,
            error=error,
            token_env_present=token_env_present,
            restorable=restorable,
        )

    present = owned_value(document, spec.document)
    scalars_present = _scalars_match(document, spec.document, expected_scalars)

    if present is None and not spec.document.owned_key_path and expected_scalars:
        # Goose and Antigravity: the footprint *is* the scalars.
        state = (
            DesktopAppState.CONFIGURED
            if scalars_present is True
            else DesktopAppState.DRIFTED
            if scalars_present is False and _any_scalar_set(document, spec.document)
            else DesktopAppState.INSTALLED
        )
        return DesktopProbe(
            app_id=spec.id,
            state=state,
            document_path=str(path),
            document_exists=exists,
            token_env_present=token_env_present,
            restorable=restorable,
        )

    expected = dict(expected_block) if expected_block is not None else None
    if expected is not None and (
        spec.document.document_format is DocumentFormat.JSON_ARRAY
    ):
        # The element MCC writes carries the field ownership is matched on,
        # which the caller's block does not: it is the spec's, not the
        # catalogue's. Adding it here rather than asking every caller to means
        # a configured VS Code cannot read as drifted on the first poll.
        expected[spec.document.match_field] = spec.document.match_value

    if present is None:
        state = DesktopAppState.INSTALLED
    elif expected is None or (present == expected and scalars_present is not False):
        state = DesktopAppState.CONFIGURED
    else:
        state = DesktopAppState.DRIFTED

    return DesktopProbe(
        app_id=spec.id,
        state=state,
        document_path=str(path),
        document_exists=exists,
        token_env_present=token_env_present,
        restorable=restorable,
    )


def _any_scalar_set(document: object, document_spec: DesktopDocument) -> bool:
    for key_path in document_spec.overwritten_keys:
        node: object = document
        for key in key_path:
            mapping = _as_object_map(node)
            if mapping is None or key not in mapping:
                node = None
                break
            node = mapping[key]
        if node is not None:
            return True
    return False


def _scalars_match(
    document: object,
    document_spec: DesktopDocument,
    expected_scalars: Mapping[str, object] | None,
) -> bool | None:
    if not expected_scalars:
        return None
    for key_path in document_spec.overwritten_keys:
        label = ".".join(key_path)
        if label not in expected_scalars:
            continue
        node: object = document
        found = True
        for key in key_path:
            mapping = _as_object_map(node)
            if mapping is None or key not in mapping:
                found = False
                break
            node = mapping[key]
        if not found or node != expected_scalars[label]:
            return False
    return True


def _mask(value: object) -> object:
    """Return a value with every credential-shaped field replaced by ``***``."""

    if isinstance(value, dict):
        return {
            key: MASK
            if str(key).lower().replace("-", "_") in _SECRET_KEYS
            else _mask(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask(item) for item in value]
    return value


def _render(document: object, document_format: DocumentFormat) -> str:
    if document_format is DocumentFormat.TOML:
        # A TOML plan is shown as the merged document's text, produced by the
        # same edits apply would make, so the diff is of the real thing.
        return ""
    return json.dumps(_mask(document), indent=2, sort_keys=False) + "\n"


def _edits_for(
    document_spec: DesktopDocument,
    block: Mapping[str, object] | None,
    scalars: Mapping[str, object],
) -> list[SetTable | SetScalar]:
    edits: list[SetTable | SetScalar] = []
    if block is not None and document_spec.owned_key_path:
        edits.append(SetTable(document_spec.owned_key_path, block))
    for key_path in document_spec.overwritten_keys:
        label = ".".join(key_path)
        if label in scalars:
            edits.append(SetScalar(key_path, scalars[label]))
    return edits


def _apply_to_object(
    document: object,
    document_spec: DesktopDocument,
    block: Mapping[str, object] | None,
    scalars: Mapping[str, object],
) -> object:
    """Return the document with MCC's edits applied, for the object formats."""

    if document_spec.document_format is DocumentFormat.JSON_ARRAY:
        elements = list(document) if isinstance(document, list) else []
        element = dict(block or {})
        element[document_spec.match_field] = document_spec.match_value
        for index, existing in enumerate(elements):
            if (
                isinstance(existing, dict)
                and existing.get(document_spec.match_field) == document_spec.match_value
            ):
                elements[index] = element
                return elements
        elements.append(element)
        return elements

    result = dict(document) if isinstance(document, dict) else {}
    if block is not None and document_spec.owned_key_path:
        _set_in(result, document_spec.owned_key_path, dict(block))
    for key_path in document_spec.overwritten_keys:
        label = ".".join(key_path)
        if label in scalars:
            _set_in(result, key_path, scalars[label])
    return result


def _remove_from_object(
    document: object,
    document_spec: DesktopDocument,
    *,
    restore: Mapping[str, object] | None,
) -> object:
    if document_spec.document_format is DocumentFormat.JSON_ARRAY:
        elements = list(document) if isinstance(document, list) else []
        return [
            element
            for element in elements
            if not (
                isinstance(element, dict)
                and element.get(document_spec.match_field) == document_spec.match_value
            )
        ]

    result = dict(document) if isinstance(document, dict) else {}
    if document_spec.owned_key_path:
        _delete_in(result, document_spec.owned_key_path)
        _prune_empty_ancestors(result, document_spec.owned_key_path)
    for key_path in document_spec.overwritten_keys:
        label = ".".join(key_path)
        if restore is not None and label in restore:
            _set_in(result, key_path, restore[label])
        else:
            _delete_in(result, key_path)
    return result


def _set_in(
    document: dict[str, object], key_path: Sequence[str], value: object
) -> None:
    node: dict[str, object] = document
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
        return document.pop(head, _MISSING) is not _MISSING
    child = _as_object_map(document.get(head))
    if child is None or not _delete_in(child, rest):
        return False
    document[head] = child
    return True


def _prune_empty_ancestors(
    document: dict[str, object], key_path: Sequence[str]
) -> None:
    """Drop containers above MCC's key that are empty only because MCC left.

    ``provider.mcc`` in a file that had no ``provider`` map at all means
    Configure created the ``provider`` key too. Deleting only the leaf would
    leave ``"provider": {}`` behind -- MCC's litter in a document it promised
    to leave alone, and enough to make a Configure/Undo cycle fail to return
    the file to what it was. An ancestor that still holds somebody else's
    provider is of course kept.
    """

    for depth in range(len(key_path) - 1, 0, -1):
        node = document
        for key in key_path[:depth]:
            child = node.get(key)
            if not isinstance(child, dict):
                return
            node = child
        if node:
            return
        parent = document
        for key in key_path[: depth - 1]:
            candidate = parent.get(key)
            if not isinstance(candidate, dict):
                return
            parent = candidate
        parent.pop(key_path[depth - 1], None)


_MISSING = object()


def _diff(before: str, after: str, path: Path) -> str:
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            n=3,
        )
    )


def plan(
    spec: DesktopAppSpec,
    *,
    env: Mapping[str, str],
    block: Mapping[str, object] | None,
    scalars: Mapping[str, object] | None = None,
    sidecar_document: Mapping[str, object] | None = None,
) -> DesktopPlan:
    """Return what Configure would write. Touches no disk."""

    if spec.document is None:
        raise DesktopApplyError(f"{spec.id} has no document to configure")

    path = document_path_for(spec, env)
    if path is None:
        raise DesktopApplyError(f"{spec.id} declares no path for this platform")

    scalars = dict(scalars or {})
    document_format = spec.document.document_format
    before_text = _read_text(path) or ""

    document, error = _load(path, document_format)
    if document is None:
        raise DesktopApplyError(f"cannot parse {path}: {error}")

    if document_format is DocumentFormat.TOML or document_format is DocumentFormat.YAML:
        after_text = apply_edits(
            before_text, document_format, _edits_for(spec.document, block, scalars)
        )
        rendered_before, rendered_after = before_text, after_text
    else:
        merged = _apply_to_object(document, spec.document, block, scalars)
        rendered_before = _render(document, document_format) if before_text else ""
        rendered_after = _render(merged, document_format)
        after_text = json.dumps(merged, indent=2) + "\n"

    sidecar_diff = ""
    sidecar_display = ""
    if spec.sidecar is not None and sidecar_document is not None:
        sidecar = sidecar_path_for(spec, env)
        if sidecar is not None:
            sidecar_display = str(sidecar)
            sidecar_before = _read_text(sidecar) or ""
            sidecar_after = json.dumps(_mask(sidecar_document), indent=2) + "\n"
            sidecar_diff = _diff(sidecar_before, sidecar_after, sidecar)

    # The object formats are masked structurally before rendering, so masking
    # their text again would only damage it -- it strips the trailing comma
    # and leaves a preview that is not the JSON it claims to be. The text
    # formats are rendered from the user's own bytes and have no other chance.
    if document_format in {DocumentFormat.TOML, DocumentFormat.YAML}:
        rendered_before = _mask_text(rendered_before)
        rendered_after = _mask_text(rendered_after)
    diff = _diff(rendered_before, rendered_after, path)

    return DesktopPlan(
        app_id=spec.id,
        document_path=str(path),
        diff=diff,
        sidecar_path=sidecar_display,
        sidecar_diff=sidecar_diff,
        overwritten_keys=tuple(
            ".".join(key_path) for key_path in spec.document.overwritten_keys
        ),
        no_op=(after_text == before_text and not sidecar_diff),
        actions=_actions_for(spec, env),
    )


_SECRET_LINE_KEYS = tuple(sorted(_SECRET_KEYS))


def _mask_text(text: str) -> str:
    """Mask credential-shaped assignments in a rendered TOML or YAML document.

    The object formats are masked structurally before rendering; the text
    formats are rendered from the user's own bytes, so masking happens per
    line. Both paths mask the same key names.
    """

    masked: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        key = stripped.split("=", 1)[0].split(":", 1)[0].strip().strip('"').lower()
        if key.replace("-", "_") in _SECRET_KEYS and (
            "=" in stripped or ":" in stripped
        ):
            indent = line[: len(line) - len(line.lstrip())]
            separator = "=" if "=" in stripped else ":"
            newline = "\n" if line.endswith("\n") else ""
            masked.append(
                f"{indent}{stripped.split(separator, 1)[0].strip()} "
                f'{separator} "{MASK}"{newline}'
            )
            continue
        masked.append(line)
    return "".join(masked)


def _actions_for(spec: DesktopAppSpec, env: Mapping[str, str]) -> tuple[str, ...]:
    """Return the lines the card shows: what to export, what to restart.

    MCC never sets a user-scope environment variable as a side effect. It is
    the one edit here that leaves both the app's file and MCC's own directory,
    and no Undo could take it back -- so the card says what to export and the
    next status poll reports whether it took.
    """

    actions: list[str] = []
    if spec.token_env_var and not env.get(spec.token_env_var):
        actions.append(
            f"Export {spec.token_env_var} in the environment "
            f"{spec.display_name} is started from. MCC does not set it for you."
        )
    if spec.restart_required:
        actions.append(f"Restart {spec.display_name} to pick this up.")
    return tuple(actions)


def _backup_once(path: Path, suffix: str) -> Path | None:
    """Copy the document to its backup, once, inheriting its mode.

    Inheriting the mode matters: a backup of a document that held a token
    holds that token, and a 0644 copy of a 0600 file is a downgrade the user
    never asked for. This is the bug two of the surveyed peers shipped.
    """

    backup_path = path.with_name(path.name + suffix)
    if backup_path.exists() or not path.exists():
        return backup_path if backup_path.exists() else None
    try:
        shutil.copyfile(path, backup_path)
        shutil.copymode(path, backup_path)
    except OSError:
        return None
    return backup_path


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".mcc-tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def _write_owned_file(path: Path, document: Mapping[str, object]) -> None:
    """Write a file MCC owns outright, restricted to its owner.

    This is the only place a literal credential may land, and only for an app
    that resolves no reference form at all -- the Kimi precedent. 0600 is the
    point of the exercise, so it is set before the content is written on POSIX
    and the file is created fresh each time on Windows, where the mode bits
    mean nothing and inheritance from the user's profile ACL is the control.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2) + "\n"
    temporary = path.with_name(path.name + ".mcc-tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    if sys.platform != "win32":
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)


def apply(
    spec: DesktopAppSpec,
    *,
    env: Mapping[str, str],
    block: Mapping[str, object] | None,
    scalars: Mapping[str, object] | None = None,
    sidecar_document: Mapping[str, object] | None = None,
    record_path: Path | None = None,
) -> DesktopWriteResult:
    """Write MCC's keys into the app's document, remembering what it replaced."""

    if spec.document is None:
        raise DesktopApplyError(f"{spec.id} has no document to configure")

    path = document_path_for(spec, env)
    if path is None:
        raise DesktopApplyError(f"{spec.id} declares no path for this platform")
    if not path.exists() and not spec.document.create_if_missing:
        raise DesktopApplyError(
            f"{spec.display_name} has not written {path} yet; open it once first"
        )

    scalars = dict(scalars or {})
    document_format = spec.document.document_format
    before_text = _read_text(path) or ""
    document, error = _load(path, document_format)
    if document is None:
        raise DesktopApplyError(f"cannot parse {path}: {error}")

    overwritten = capture_overwritten(document, spec.document.overwritten_keys)

    if document_format in {DocumentFormat.TOML, DocumentFormat.YAML}:
        after_text = apply_edits(
            before_text, document_format, _edits_for(spec.document, block, scalars)
        )
    else:
        merged = _apply_to_object(document, spec.document, block, scalars)
        after_text = json.dumps(merged, indent=2) + "\n"

    sidecar_display = ""
    sidecar_changed = False
    if spec.sidecar is not None and sidecar_document is not None:
        sidecar = sidecar_path_for(spec, env)
        if sidecar is not None:
            sidecar_display = str(sidecar)
            desired = json.dumps(sidecar_document, indent=2) + "\n"
            if (_read_text(sidecar) or "") != desired:
                _write_owned_file(sidecar, sidecar_document)
                sidecar_changed = True

    if after_text == before_text and not sidecar_changed:
        return DesktopWriteResult(
            app_id=spec.id,
            changed=False,
            document_path=str(path),
            sidecar_path=sidecar_display,
        )

    backup_path = (
        _backup_once(path, spec.document.backup_suffix) if before_text else None
    )
    if after_text != before_text:
        _write_text(path, after_text)

    write_entry(
        RestoreEntry(
            subject=spec.id,
            document_path=str(path),
            document_sha256=document_sha256(path),
            overwritten=overwritten,
        ),
        path=record_path,
    )

    return DesktopWriteResult(
        app_id=spec.id,
        changed=True,
        document_path=str(path),
        backup_path=str(backup_path) if backup_path else "",
        sidecar_path=sidecar_display,
    )


def undo(
    spec: DesktopAppSpec,
    *,
    env: Mapping[str, str],
    mode: UndoMode = UndoMode.KEYS_ONLY,
    record_path: Path | None = None,
) -> DesktopWriteResult:
    """Take MCC's keys back out, optionally restoring what MCC replaced."""

    if spec.document is None:
        raise DesktopApplyError(f"{spec.id} has no document to undo")

    path = document_path_for(spec, env)
    if path is None:
        raise DesktopApplyError(f"{spec.id} declares no path for this platform")

    before_text = _read_text(path) or ""
    document_format = spec.document.document_format
    document, error = _load(path, document_format)
    if document is None:
        raise DesktopApplyError(f"cannot parse {path}: {error}")

    restore: dict[str, object] | None = None
    restored_keys: list[str] = []
    if mode is UndoMode.RESTORE and spec.document.overwritten_keys:
        # An app whose spec overwrites nothing has nothing to restore, and for
        # it the two modes are the same operation. Only an app that *can* have
        # replaced a value is allowed to fail for want of a record -- otherwise
        # picking "Restore the original values" on Command Code or OpenCode
        # would be an error message for a request that was already satisfied.
        entry = read_entry(spec.id, path=record_path)
        if entry is None:
            raise DesktopApplyError(
                f"no record of what MCC replaced for {spec.display_name}. "
                "Undo can still remove MCC's keys."
            )
        current = document_sha256(path)
        if entry.document_sha256 and current and entry.document_sha256 != current:
            raise DesktopApplyError(
                f"{path} has changed since MCC configured it, so restoring the "
                "pre-MCC values would overwrite an edit made since. Remove "
                "MCC's keys only, or restore from "
                f"{path.name}{spec.document.backup_suffix} by hand."
            )
        restore = {}
        for value in entry.overwritten:
            if value.prior_present:
                restore[".".join(value.key_path)] = value.prior_value
                restored_keys.append(".".join(value.key_path))

    if document_format in {DocumentFormat.TOML, DocumentFormat.YAML}:
        edits: list[SetTable | SetScalar | DeleteKey] = []
        if spec.document.owned_key_path:
            edits.append(DeleteKey(spec.document.owned_key_path))
        for key_path in spec.document.overwritten_keys:
            label = ".".join(key_path)
            if restore is not None and label in restore:
                edits.append(SetScalar(key_path, restore[label]))
            else:
                edits.append(DeleteKey(key_path))
        after_text = apply_edits(before_text, document_format, edits)
    else:
        stripped = _remove_from_object(document, spec.document, restore=restore)
        after_text = json.dumps(stripped, indent=2) + "\n"
        if not before_text:
            after_text = before_text

    removed_sidecar = False
    sidecar_display = ""
    sidecar = sidecar_path_for(spec, env)
    if sidecar is not None:
        sidecar_display = str(sidecar)
        if sidecar.exists():
            try:
                sidecar.unlink()
                removed_sidecar = True
            except OSError:
                removed_sidecar = False

    changed = after_text != before_text
    if changed:
        _backup_once(path, spec.document.backup_suffix)
        _write_text(path, after_text)

    forget_entry(spec.id, path=record_path)

    return DesktopWriteResult(
        app_id=spec.id,
        changed=changed or removed_sidecar,
        document_path=str(path),
        sidecar_path=sidecar_display,
        removed_sidecar=removed_sidecar,
        restored_keys=tuple(restored_keys),
    )


def _as_object_map(value: object) -> dict[str, object] | None:
    """Return a mapping as a string-keyed dict, or None when it is not one.

    ``isinstance(value, dict)`` narrows only to ``dict[Unknown, Unknown]``, and
    ``dict`` is invariant, so the narrowed value is not usable as a
    ``dict[str, object]``. Reconstructing is what makes the key type real --
    the same answer ``config/harness_config_merge.py`` reached for the same
    reason, and the reason both are spelled out rather than asserted away.
    """

    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


__all__ = [
    "DesktopApplyError",
    "DesktopPlan",
    "DesktopProbe",
    "DesktopSidecar",
    "DesktopWriteResult",
    "apply",
    "document_path_for",
    "is_installed",
    "plan",
    "probe",
    "sidecar_path_for",
    "undo",
]
