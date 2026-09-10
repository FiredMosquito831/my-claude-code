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

**The two undo modes.** Both remove what MCC owns and both put back a value
MCC *replaced*; what separates them is what they refuse.

``KEYS_ONLY``
    Remove MCC's own keys, and where MCC overwrote a value the user already
    had, put that value back. Never refuses: no restore record and no hash
    check, so it is always available. This is the default, and the mode a card
    offers when the other cannot be trusted.

``RESTORE``
    The same, but it *guarantees* the pre-MCC state: it requires the record and
    refuses when the document has been rewritten since MCC wrote it, rather
    than reverting an edit the user made on purpose. It also consumes the
    record; ``KEYS_ONLY`` leaves it in place.

Until 6.56.0 ``KEYS_ONLY`` deleted every key named in ``overwritten_keys``,
including one that had held a value of the user's own, and then dropped the
record -- so a Codex user's default ``model`` line was deleted outright and
"Restore the original values" answered 409 afterwards. ``model`` was never
MCC's key, and the mode's own label said so.

**A source that outranks the file.** :func:`managed_override` asks, before
anything else, whether some higher-precedence configuration owns this app --
a Windows policy key, a macOS managed profile. Where one does, the probe
reports ``managed`` and Configure refuses, because a file that is written
successfully and then ignored is the same invisible failure as a file written
to the wrong path.
"""

import json
import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import unified_diff
from pathlib import Path

from my_claude_code.config.desktop_apps import (
    PLATFORM_ENV_VARS,
    DesktopAppSpec,
    DesktopAppState,
    DesktopAppStatus,
    DesktopDocument,
    DesktopManagedSource,
    DesktopPath,
    DesktopSidecar,
    TokenForm,
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
    #: The higher-precedence source that owns this app's configuration, or "".
    managed_by: str = ""
    #: The keys that source sets, so the card can say what is being enforced.
    managed_keys: tuple[str, ...] = ()
    #: One line per legacy identity this probe repaired, for the card to show.
    #: Empty on every probe after the first, and on every machine that never
    #: had the broken configuration -- including one a user repaired by hand.
    repaired: tuple[str, ...] = ()


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
        # Claude Desktop's own name for the gateway credential. It does not
        # contain "api_key" as a whole-key match, and a missed mask is a
        # leaked key -- this is the field MCC writes a literal into.
        "inferencegatewayapikey",
        # Codex's field for a literal bearer token. Same reasoning: it is not
        # spelled "api_key", and since 6.67.0 it is where MCC's proxy token
        # goes in ~/.codex/config.toml -- a TOML document, so it is the
        # line-based masker in ``_mask_text`` that has to catch it too.
        "experimental_bearer_token",
    }
)


def _platform() -> str:
    return sys.platform


def _base_directory(
    candidate: DesktopPath, env: Mapping[str, str]
) -> tuple[Path | None, bool]:
    """Return ``(directory, app_directed)`` for one declared path.

    ``app_directed`` is true when the variable that supplied the directory is
    one the *app* defines for its own configuration -- ``CODEX_HOME``,
    ``OPENCODE_CONFIG_DIR``, ``CRUSH_GLOBAL_CONFIG``. That flag is what lets
    "prefer a path that already exists" coexist with those variables: setting
    one is the user saying where their config is, and it has to win even when
    the file has not been created yet.
    """

    if candidate.in_mcc_config_dir:
        return config_dir_path(), False
    for name in candidate.env_vars:
        value = env.get(name, "").strip()
        if value:
            return Path(value), name.upper() not in PLATFORM_ENV_VARS
    if not candidate.env_vars:
        return Path.home(), False
    return None, False


def _resolve_candidate(
    candidate: DesktopPath, env: Mapping[str, str]
) -> tuple[Path | None, bool]:
    """Return one declared path resolved on this machine, and its directedness.

    A ``glob`` candidate resolves to the first match in sorted order and to
    ``None`` when nothing matches -- which is the answer a detection marker
    wants: no ``Packages/Claude_*`` directory means the packaged install is not
    there. A ``relative_parts`` beginning with an absolute segment (the macOS
    and Linux managed-configuration paths) resets the join to the filesystem
    root, which is pathlib's documented behaviour and the reason those entries
    need no separate field.
    """

    base, directed = _base_directory(candidate, env)
    if base is None:
        return None, False
    if candidate.glob:
        pattern = "/".join(candidate.relative_parts)
        try:
            matches = sorted(base.glob(pattern))
        except OSError:
            return None, directed
        if not matches:
            return None, directed
        return matches[0].absolute(), directed
    return base.joinpath(*candidate.relative_parts).absolute(), directed


def resolve_path(paths: Sequence[DesktopPath], env: Mapping[str, str]) -> Path | None:
    """Return the declared path this app actually reads on this machine.

    Three rules, applied in order, and the second is the one that was missing:

    1. A path supplied by a variable the *app* defines for its own config
       location wins outright. Following the app's own lookup order is the only
       way the file MCC writes is the file the app reads.
    2. Otherwise, a declared path that **already exists** beats one that does
       not. Without this, a first-platform-match rule sent every Windows
       OpenCode Configure at ``%APPDATA%\\opencode\\opencode.json`` -- a file
       OpenCode does not read -- created it, and reported success, while the
       real config sat in ``~/.config/opencode``. The mistake is invisible from
       the dashboard and shows up only as a model picker that never lists MCC.
    3. Otherwise, the first declared path that resolves at all, which is where
       Configure creates the file.
    """

    platform = _platform()
    applicable = [
        candidate
        for candidate in paths
        if not candidate.platforms or platform in candidate.platforms
    ]
    resolved = [
        (candidate, *_resolve_candidate(candidate, env)) for candidate in applicable
    ]

    for _candidate, path, directed in resolved:
        if path is not None and directed:
            return path
    for _candidate, path, _directed in resolved:
        if path is not None and path.exists():
            return path
    for _candidate, path, _directed in resolved:
        if path is not None:
            return path
    for candidate in applicable:
        if not candidate.glob:
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


def _registry_value_names(hive: str, subkey: str) -> tuple[str, ...]:
    """Return the value names sitting *directly* under one policy key.

    Empty on any platform without a registry, on a key that does not exist,
    and on any error at all: this probe decides whether to *disable* a button,
    so a failure to read has to mean "no evidence of management", never
    "assume managed" (which would make the feature unusable on a machine whose
    registry is merely locked down) and never an exception (which would take
    the whole card down with it).

    Only direct values are read, and that is Claude Desktop's own rule rather
    than a shortcut: its documentation says the app never reads values nested
    in a subkey, so a subkey full of settings is not configuration and must not
    count as management.
    """

    if sys.platform != "win32":
        return ()
    try:
        import winreg
    except ImportError:  # pragma: no cover - win32 always has it
        return ()
    roots = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
    root = roots.get(hive.upper())
    if root is None:
        return ()
    names: list[str] = []
    try:
        with winreg.OpenKey(root, subkey) as key:
            index = 0
            while True:
                try:
                    name, _value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                names.append(str(name))
                index += 1
    except OSError:
        return ()
    return tuple(names)


def _managed_file_keys(path: Path) -> tuple[str, ...]:
    """Return the top-level key names of a managed JSON or plist file."""

    if not path.is_file():
        return ()
    if path.suffix == ".plist":
        try:
            import plistlib

            with path.open("rb") as handle:
                loaded = plistlib.load(handle)
        except OSError, ValueError:
            return ()
        mapping = _as_object_map(loaded)
        return tuple(mapping) if mapping else ()
    text = _read_text(path)
    if not text:
        return ()
    try:
        loaded = json.loads(text)
    except ValueError:
        return ()
    mapping = _as_object_map(loaded)
    return tuple(mapping) if mapping else ()


def _source_governs(
    source: DesktopManagedSource, env: Mapping[str, str]
) -> tuple[bool, tuple[str, ...]]:
    """Return whether one managed source owns the settings MCC would write."""

    if source.registry_hive:
        names = _registry_value_names(source.registry_hive, source.registry_subkey)
    elif source.path is not None:
        platform = _platform()
        if source.path.platforms and platform not in source.path.platforms:
            return False, ()
        resolved = resolve_path((source.path,), env)
        names = _managed_file_keys(resolved) if resolved is not None else ()
    else:
        return False, ()
    exempt = {key.lower() for key in source.exempt_keys}
    governing = tuple(name for name in names if name.lower() not in exempt)
    return bool(governing), governing


def managed_override(
    spec: DesktopAppSpec, env: Mapping[str, str]
) -> tuple[str, tuple[str, ...]]:
    """Return ``(label, keys)`` of the source that outranks MCC's file, or ``("", ())``.

    Highest-precedence source first, which is the order they are declared in.
    """

    for source in spec.managed_sources:
        governs, keys = _source_governs(source, env)
        if governs:
            return source.label, keys
    return "", ()


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

    if document_spec.owned_element_path:
        for element in _element_list(document, document_spec.owned_element_path):
            mapping = _as_object_map(element)
            if (
                mapping is not None
                and mapping.get(document_spec.match_field) == document_spec.match_value
            ):
                return mapping
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


def repair_legacy(spec: DesktopAppSpec, env: Mapping[str, str]) -> tuple[str, ...]:
    """Move what an earlier MCC wrote under a rejected identity onto the current one.

    Returns one line per repair performed, and an **empty tuple whenever there
    is nothing MCC recognises as its own old work** -- which is the property
    that matters most here. Every step below is conditioned on *finding* the
    legacy value, never on asserting the new one, so three states come out
    identical and untouched: a machine that never pressed Configure, a machine
    already repaired by an earlier poll, and a machine whose user repaired it
    by hand (the case on the machine this was written for -- the user deleted
    MCC's file and put ``appliedId`` back on their own entry themselves on
    2026-09-09; a repair that "fixed" that would be a second incident).

    Three things happen, in this order, and only for a legacy value actually
    present:

    1. A file MCC owned under the old identity is copied to the current path
       when nothing is there yet, then deleted. Copying rather than waiting for
       the next Configure is what makes the app work again on the next launch:
       the content is MCC's own, it was correct, and only the *name* of the
       file was wrong.
    2. An element of the owned list carrying the old id is removed; Configure
       will add the current one, and until it does the index simply does not
       mention a document that no longer exists.
    3. An overwritten scalar still *pointing at* the old id -- Claude Desktop's
       ``appliedId`` -- is moved to the current one. A scalar pointing anywhere
       else is somebody's own choice and is left alone.
    """

    if not spec.legacy_entries or spec.document is None:
        return ()

    path = document_path_for(spec, env)
    if path is None:
        return ()

    current_sidecar = sidecar_path_for(spec, env)
    repairs: list[str] = []
    moved_files = False

    for legacy in spec.legacy_entries:
        legacy_path = (
            resolve_path(legacy.sidecar_paths, env) if legacy.sidecar_paths else None
        )
        if legacy_path is not None and legacy_path.is_file():
            if current_sidecar is not None and not current_sidecar.exists():
                try:
                    current_sidecar.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(legacy_path, current_sidecar)
                    shutil.copymode(legacy_path, current_sidecar)
                except OSError:
                    continue
            try:
                legacy_path.unlink()
            except OSError:
                continue
            moved_files = True

    before_text = _read_text(path) or ""
    document, error = _load(path, spec.document.document_format)
    changed = False
    if document is not None and not error:
        result = _as_object_map(document)
        if result is not None:
            for legacy in spec.legacy_entries:
                if _rename_legacy_element(result, spec.document, legacy.match_value):
                    changed = True
                if _repoint_legacy_scalar(result, spec.document, legacy.match_value):
                    changed = True
            if changed:
                _backup_library_once(path, spec)
                _write_text(path, _json_text(result, before_text))

    if changed or moved_files:
        repairs.extend(legacy.reason for legacy in spec.legacy_entries if legacy.reason)
    return tuple(repairs)


def _rename_legacy_element(
    document: dict[str, object], document_spec: DesktopDocument, legacy_value: str
) -> bool:
    """Move the owned-list element off a legacy match value onto the current one.

    Renamed in place rather than removed, and that is not cosmetic. Claude
    Desktop's loader builds the path from ``appliedId`` alone, so a repaired
    library would *load* either way -- but ``entries`` is what its
    configuration picker lists, and an applied configuration missing from the
    picker is a user who cannot see, name or switch away from what their
    machine is using. The element keeps its position and its label; only the id
    changes. Where an element already carries the current value the legacy one
    is dropped instead, because two elements with one id is a worse index than
    none.
    """

    if not document_spec.owned_element_path:
        return False
    elements = _element_list(document, document_spec.owned_element_path)
    current_present = any(
        (mapping := _as_object_map(element)) is not None
        and mapping.get(document_spec.match_field) == document_spec.match_value
        for element in elements
    )
    changed = False
    rebuilt: list[object] = []
    for element in elements:
        mapping = _as_object_map(element)
        if mapping is None or mapping.get(document_spec.match_field) != legacy_value:
            rebuilt.append(element)
            continue
        changed = True
        if current_present:
            continue
        mapping[document_spec.match_field] = document_spec.match_value
        rebuilt.append(mapping)
        current_present = True
    if not changed:
        return False
    _set_in(document, document_spec.owned_element_path, rebuilt)
    return True


def _repoint_legacy_scalar(
    document: dict[str, object], document_spec: DesktopDocument, legacy_value: str
) -> bool:
    """Move an overwritten scalar off a legacy match value onto the current one."""

    changed = False
    for key_path in document_spec.overwritten_keys:
        node: object = document
        for key in key_path:
            mapping = _as_object_map(node)
            if mapping is None or key not in mapping:
                node = None
                break
            node = mapping[key]
        if node == legacy_value:
            _set_in(document, key_path, document_spec.match_value)
            changed = True
    return changed


def probe(
    spec: DesktopAppSpec,
    *,
    env: Mapping[str, str],
    expected_block: Mapping[str, object] | None = None,
    expected_scalars: Mapping[str, object] | None = None,
    expected_sidecar: Mapping[str, object] | None = None,
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
    managed_by, managed_keys = managed_override(spec, env)

    # The one place this function writes, and it writes only what an earlier
    # MCC wrote wrongly. It runs on probe rather than on Configure because a
    # user whose Claude Desktop was silently un-configured has no reason to
    # press Configure again -- they were told it was already configured -- so a
    # repair that waited for the button would never run on the machines that
    # need it. It is skipped where a managed source outranks the file, since
    # nothing MCC does there has any effect. See :func:`repair_legacy` for why
    # an already-repaired library comes out untouched.
    repaired = () if managed_by else repair_legacy(spec, env)

    if managed_by:
        # Checked before the document is read, and before "installed" matters:
        # what a managed source makes true is that the file MCC would write is
        # not the file the app obeys, whatever that file currently says.
        return DesktopProbe(
            app_id=spec.id,
            state=DesktopAppState.MANAGED,
            document_path=str(document_path_for(spec, env) or ""),
            document_exists=False,
            token_env_present=token_env_present,
            restorable=restorable,
            repaired=repaired,
            managed_by=managed_by,
            managed_keys=managed_keys,
        )

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
            repaired=repaired,
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
            repaired=repaired,
        )

    present = owned_value(document, spec.document)
    scalars_present = _scalars_match(document, spec.document, expected_scalars)

    if (
        present is None
        and not spec.document.owned_key_path
        and not spec.document.owned_element_path
        and expected_scalars
    ):
        # Goose and Antigravity: the footprint *is* the scalars. Not Claude
        # Desktop, whose footprint is an element of ``entries`` plus the
        # ``appliedId`` that points at it -- with this branch taken, a library
        # holding only the user's own configuration read as *drifted*, which
        # says MCC wrote something here and it has changed. Nothing had.
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
            repaired=repaired,
        )

    expected = dict(expected_block) if expected_block is not None else None
    if expected is not None and (
        spec.document.document_format is DocumentFormat.JSON_ARRAY
        or spec.document.owned_element_path
    ):
        # The element MCC writes carries the field ownership is matched on,
        # which the caller's block does not: it is the spec's, not the
        # catalogue's. Adding it here rather than asking every caller to means
        # a configured VS Code cannot read as drifted on the first poll.
        expected[spec.document.match_field] = spec.document.match_value

    if present is None:
        state = DesktopAppState.INSTALLED
    elif expected is None or (
        present == expected
        and scalars_present is not False
        and _sidecar_matches(spec, env, expected_sidecar) is not False
    ):
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
        repaired=repaired,
    )


def _sidecar_matches(
    spec: DesktopAppSpec,
    env: Mapping[str, str],
    expected_sidecar: Mapping[str, object] | None,
) -> bool | None:
    """Return whether the file MCC owns outright is what a re-apply would write.

    ``None`` when there is nothing to compare. This exists because for an app
    whose real settings live in the sidecar -- Claude Desktop, whose entry in
    the user's document is only a label and an id -- comparing the user's
    document alone could never report ``drifted``: hand-editing the gateway URL
    would change nothing MCC looks at, and the card would keep saying
    "Configured by MCC" about a machine pointed somewhere else.
    """

    if spec.sidecar is None or expected_sidecar is None:
        return None
    path = sidecar_path_for(spec, env)
    if path is None:
        return None
    text = _read_text(path)
    if text is None:
        return False
    try:
        loaded = json.loads(text)
    except ValueError:
        return False
    return _as_object_map(loaded) == dict(expected_sidecar)


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
    if block is not None and document_spec.owned_element_path:
        # One element of a list nested in an object: MCC's entry is replaced
        # in place where it is already there and appended where it is not, so
        # the user's own entries keep their order and their bytes.
        elements = list(_element_list(result, document_spec.owned_element_path))
        element = dict(block)
        element[document_spec.match_field] = document_spec.match_value
        replaced = False
        for index, existing in enumerate(elements):
            mapping = _as_object_map(existing)
            if (
                mapping is not None
                and mapping.get(document_spec.match_field) == document_spec.match_value
            ):
                elements[index] = element
                replaced = True
                break
        if not replaced:
            elements.append(element)
        _set_in(result, document_spec.owned_element_path, elements)
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
    if document_spec.owned_element_path:
        kept = [
            element
            for element in _element_list(result, document_spec.owned_element_path)
            if not _is_owned_element(element, document_spec)
        ]
        _set_in(result, document_spec.owned_element_path, kept)
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


def _element_list(document: object, key_path: Sequence[str]) -> list[object]:
    """Return the list nested at ``key_path``, or an empty list."""

    node: object = document
    for key in key_path:
        mapping = _as_object_map(node)
        if mapping is None or key not in mapping:
            return []
        node = mapping[key]
    return list(node) if isinstance(node, list) else []


def _is_owned_element(element: object, document_spec: DesktopDocument) -> bool:
    mapping = _as_object_map(element)
    return (
        mapping is not None
        and mapping.get(document_spec.match_field) == document_spec.match_value
    )


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


def _json_text(document: object, before_text: str) -> str:
    """Serialise an object document, keeping the file's trailing-newline habit.

    Everything else about an object document is normalised by a round trip
    through ``json`` -- that is the price of the format, and every app here
    pays it. The trailing newline need not be: Claude Desktop writes its own
    ``_meta.json`` without one, so appending one left a Configure/Undo cycle
    one byte away from where it started. A promise to return a file to what
    it was is worth keeping exactly.
    """

    text = json.dumps(document, indent=2)
    if before_text and not before_text.endswith("\n"):
        return text
    return text + "\n"


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
        after_text = _json_text(merged, before_text)

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


#: Where whole-directory backups go: MCC's own configuration directory, never
#: beside the original. Claude Desktop globs ``*.json`` out of its
#: configuration library, so a copy of the library left *inside* the library is
#: a set of documents the app may try to read.
LIBRARY_BACKUP_DIR = "backups"


def _backup_library_once(path: Path, spec: DesktopAppSpec) -> Path | None:
    """Copy the whole directory a document lives in, once, before any edit.

    For an app whose configuration is a *directory* -- Claude Desktop's library
    is a set of documents plus an index, and what MCC merges is only the index
    -- the per-file ``.mcc-backup`` cannot restore what was there. On
    2026-09-08 that was not theoretical: Configure moved the applied id onto an
    id the app rejects at boot, and the only copy of anything was of the one
    file that was easiest to reconstruct.

    Once, and dated: a second Configure must not overwrite the copy taken
    before the first, which is the same promise :func:`_backup_once` makes one
    level down. The check is for any existing backup of this app rather than
    for one exact name, so the guarantee survives the clock.
    """

    if spec.document is None or not spec.document.backup_directory:
        return None
    source = path.parent
    if not source.is_dir():
        return None
    root = config_dir_path() / LIBRARY_BACKUP_DIR
    try:
        existing = sorted(root.glob(f"{spec.id}-*"))
    except OSError:
        existing = []
    if existing:
        return existing[0]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    destination = root / f"{spec.id}-{source.name}-{stamp}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    except OSError:
        return None
    return destination


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


def _write_text(path: Path, text: str, *, owner_only: bool = False) -> None:
    """Write a document atomically, optionally restricting it to its owner.

    ``owner_only`` is set for a document MCC has just put a literal credential
    into -- the apps that resolve no usable reference form. It is a *tightening*
    of a file the user owns, applied where the OS makes it mean something; on
    Windows the mode bits do not, and inheritance from the user's profile ACL
    is the control, exactly as in :func:`_write_owned_file`.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".mcc-tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    if owner_only and sys.platform != "win32":
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
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

    managed_by, managed_keys = managed_override(spec, env)
    if managed_by:
        keys = ", ".join(managed_keys)
        raise DesktopApplyError(
            f"{spec.display_name} is configured by {managed_by}, which "
            f"outranks the file MCC writes. Setting: {keys}. MCC will not "
            "write a file the app is going to ignore -- ask whoever manages "
            "this device, or remove the managed profile first."
        )

    path = document_path_for(spec, env)
    if path is None:
        raise DesktopApplyError(f"{spec.id} declares no path for this platform")
    if not path.exists() and not spec.document.create_if_missing:
        raise DesktopApplyError(
            f"{spec.display_name} has not written {path} yet; open it once first"
        )

    scalars = dict(scalars or {})
    document_format = spec.document.document_format
    # Read from the spec rather than taken as an argument: whether this app's
    # own document ends up holding MCC's token is a property of the app, and
    # ``config`` may not import the module that builds the block.
    holds_literal_credential = spec.token_form is TokenForm.LITERAL_IN_APP_FILE
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
        after_text = _json_text(merged, before_text)

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

    # The whole directory first, for an app whose configuration is one, then
    # the single file. Both are once-only and neither overwrites what an
    # earlier Configure took.
    _backup_library_once(path, spec)
    backup_path = (
        _backup_once(path, spec.document.backup_suffix) if before_text else None
    )
    if after_text != before_text:
        _write_text(path, after_text, owner_only=holds_literal_credential)

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
    """Take MCC's keys back out, putting back whatever MCC replaced.

    See the module docstring for what separates the two modes: both restore a
    replaced value, and only ``RESTORE`` refuses when it cannot promise the
    pre-MCC state.
    """

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
    if mode is UndoMode.KEYS_ONLY and spec.document.overwritten_keys:
        # The data-loss fix. "Remove MCC's keys only" used to delete every key
        # in ``overwritten_keys`` outright -- including one that had held the
        # user's own value before MCC replaced it. On Codex that deleted the
        # user's default ``model`` line, and the record was then dropped, so
        # "Restore the original values" answered 409 and the value was gone
        # from the UI for good. ``model`` was never MCC's key.
        #
        # So this mode now puts back what MCC replaced, exactly as RESTORE
        # does, and deletes only the keys MCC created (``prior_present`` false).
        # It differs from RESTORE in what it *refuses*: nothing. No record and
        # no hash check, because this is the mode that must always be
        # available -- it is the one offered when the other cannot be trusted.
        entry = read_entry(spec.id, path=record_path)
        if entry is not None:
            restore = {}
            for value in entry.overwritten:
                if value.prior_present:
                    restore[".".join(value.key_path)] = value.prior_value
                    restored_keys.append(".".join(value.key_path))
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
        after_text = _json_text(stripped, before_text)
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
    # And anything MCC owned under an identity it has since stopped using. Undo
    # promises to remove what MCC left behind, and a file written by an earlier
    # release is still what MCC left behind -- a user who reaches for Undo
    # because the app is misbehaving should not have to know which version
    # wrote which filename.
    for legacy in spec.legacy_entries:
        legacy_path = (
            resolve_path(legacy.sidecar_paths, env) if legacy.sidecar_paths else None
        )
        if legacy_path is not None and legacy_path.is_file():
            try:
                legacy_path.unlink()
                removed_sidecar = True
            except OSError:
                pass

    changed = after_text != before_text
    if changed:
        _backup_library_once(path, spec)
        _backup_once(path, spec.document.backup_suffix)
        _write_text(path, after_text)

    if mode is UndoMode.RESTORE:
        # Consumed: the values are back in the document, so the record has
        # done its job. KEYS_ONLY deliberately keeps it. The record answers
        # "what did the user have before MCC ever wrote here?", and that
        # question survives an undo -- dropping the answer after a keys-only
        # undo is what disarmed the second option of the picker and made the
        # data loss unrecoverable through the UI.
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
    "repair_legacy",
    "sidecar_path_for",
    "undo",
]
