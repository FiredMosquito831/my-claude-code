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

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.harnesses import (
    COMMANDCODE_BASE_URL_SENTINEL,
    HarnessConfigMerge,
)

#: Keys the serialiser puts at the top of its own document that belong inside
#: the owned subtree once merged, so removing MCC's key removes every trace of
#: MCC from the user's file. ``_mcc_defaulted`` is the record of which numbers
#: the CLI guessed; leaving it at the document root would litter a file MCC
#: does not own.
CARRIED_DOCUMENT_KEYS: tuple[str, ...] = ("_mcc_defaulted",)


@dataclass(frozen=True, slots=True)
class MergeResult:
    """What one merge did to the user's document."""

    path: Path
    changed: bool
    created: bool
    backup_path: Path | None


def merge_config_path(merge: HarnessConfigMerge, env: Mapping[str, str]) -> Path:
    """Return the file this CLI reads, resolved the way the CLI resolves it."""

    for name in merge.home_env_vars:
        value = env.get(name, "").strip()
        if value:
            return Path(value).joinpath(*merge.relative_parts)
    return Path.home().joinpath(*merge.relative_parts)


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
    block: Mapping[str, object], proxy_root_url: str
) -> dict[str, object]:
    """Return the owned block with the base-URL sentinel replaced.

    Command Code refuses a ``baseURL`` that is not a parseable absolute URL
    and applies no environment substitution to it, so the real proxy root has
    to be written literally. It is not a secret -- it is a loopback address on
    the user's own machine -- and it is the only MCC value that reaches the
    file. The API key beside it stays a ``$VAR`` reference.
    """

    resolved = dict(block)
    if resolved.get("baseURL") == COMMANDCODE_BASE_URL_SENTINEL:
        stripped = proxy_root_url.rstrip("/")
        resolved["baseURL"] = stripped if stripped.endswith("/v1") else f"{stripped}/v1"
    return resolved


def read_document(path: Path) -> dict[str, object] | None:
    """Return the user's document, or None when it is absent or unreadable."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def owned_block_present(path: Path, owned_key_path: Sequence[str]) -> bool:
    """Return whether MCC's key already exists in the user's document.

    The background refresh keys off this rather than off the file existing: a
    user with their own ``providers.json`` and no interest in MCC must never
    have a provider block appear in it because a provider key rotated.
    """

    document = read_document(path)
    if document is None:
        return False
    node: object = document
    for key in owned_key_path:
        if not isinstance(node, Mapping):
            return False
        node = node.get(key)
    return isinstance(node, Mapping)


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
