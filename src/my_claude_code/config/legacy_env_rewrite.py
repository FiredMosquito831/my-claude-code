"""The one-time ``FCC_*`` -> ``MCC_*`` rewrite of a managed ``.env`` (7.0.0).

Every ``FCC_*`` environment name was an alias of an ``MCC_*`` name. 7.0.0
removed the alias surface, which leaves exactly one way for a user to lose a
setting they configured: a managed ``.env`` that still spells a key the old way.
Nothing would read it, and the next Save in the dashboard would drop it (the
owned-prefix rule in ``config.admin.persistence`` no longer recognises ``FCC_``).

So the first 7.x start rewrites the file, once, before anything reads it:

* every ``FCC_<NAME>`` assignment becomes ``MCC_<NAME>``, byte-for-byte the same
  value, the same quoting, the same trailing comment, the same line ending;
* the original file is copied to ``<path>.bak-<YYYYmmdd-HHMMSS>`` first;
* one INFO line is logged per renamed key -- names only, never values;
* when ``MCC_<NAME>`` is already present the ``FCC_<NAME>`` line is left exactly
  where it is and named in a WARNING: it was already being overridden by the
  canonical key, and rewriting it would produce two lines for one setting.

Idempotent by construction: the second start finds no ``FCC_`` assignment, makes
no backup and logs nothing.
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from .env_migrations import _DOTENV_ASSIGNMENT_RE, _defines_key, _has_unterminated_quote

LEGACY_ENV_PREFIX = "FCC_"
CANONICAL_ENV_PREFIX = "MCC_"

#: Every ``FCC_*`` name the product itself ever read, enumerated from the
#: pre-7.0.0 sources so the rewrite can be proved exact rather than assumed:
#:
#: * ``FCC_OPEN_BROWSER`` -- the last surviving ``AliasChoices`` entry on any
#:   ``Settings`` field (``config/settings.py``, ``open_admin_browser``);
#: * ``FCC_ENV_FILE`` -- the explicit dotenv override alias
#:   (``config/env_files.py``, ``LEGACY_DOTENV_FILE_ENV``);
#: * ``FCC_SMOKE_TARGETS`` -- the developer-tooling key the owned-prefix rule in
#:   ``config/admin/persistence.py`` existed to preserve.
#:
#: The rewrite is prefix-based, so it covers names outside this list too; the
#: list is what ``tests/config/test_legacy_env_rewrite.py`` pins, and no entry
#: may be removed from it without a release note.
PINNED_LEGACY_ENV_KEYS: tuple[str, ...] = (
    "FCC_ENV_FILE",
    "FCC_OPEN_BROWSER",
    "FCC_SMOKE_TARGETS",
)


def canonical_name(legacy_key: str) -> str:
    """Return the ``MCC_*`` name that replaces a ``FCC_*`` name."""

    return CANONICAL_ENV_PREFIX + legacy_key[len(LEGACY_ENV_PREFIX) :]


def _backup_path(path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.name}.bak-{stamp}")


def rewrite_legacy_env_keys(
    path: Path, now: datetime | None = None
) -> tuple[tuple[str, str], ...]:
    """Rename every ``FCC_*`` key in ``path`` to ``MCC_*``, once.

    Returns the ``(old, new)`` pairs that were renamed -- empty when there was
    nothing to do, which is every start after the first. Never raises: a config
    home that cannot be rewritten must still start a server.
    """

    try:
        if not path.is_file():
            return ()
        # ``newline=""`` so CRLF stays CRLF: this file is edited by hand on
        # Windows and a whole-file line-ending flip would show up as a 300-line
        # diff in the user's own editor.
        with path.open("r", encoding="utf-8", newline="") as handle:
            original = handle.read()
    except OSError as exc:
        logger.warning("ENV REWRITE: cannot read {}: {}", path, exc)
        return ()

    lines = original.splitlines(keepends=True)
    renamed: list[tuple[str, str]] = []
    shadowed: list[str] = []
    rewritten: list[str] = []
    for line in lines:
        match = _DOTENV_ASSIGNMENT_RE.match(line)
        key = None if match is None else match.group("key")
        if match is None or key is None or not key.startswith(LEGACY_ENV_PREFIX):
            rewritten.append(line)
            continue
        new_key = canonical_name(key)
        if _defines_key(original, new_key):
            # The canonical key already wins. Renaming would leave two live
            # lines for one setting; dropping would be the silent loss this
            # whole module exists to prevent. Say so and leave it alone.
            shadowed.append(key)
            rewritten.append(line)
            continue
        rewritten.append(
            f"{match.group('prefix')}{new_key}{match.group('suffix')}"
            f"{line[match.end() :]}"
        )
        renamed.append((key, new_key))

    for key in shadowed:
        logger.warning(
            "ENV REWRITE: {} in {} is ignored -- {} is already set in the same "
            "file and takes effect. Delete the {} line when you are sure.",
            key,
            path,
            canonical_name(key),
            key,
        )

    if not renamed:
        return ()

    if _has_unterminated_quote(original):
        # Line-based matching cannot see where an unterminated quoted value
        # ends, so a rename could land inside the quoted text and surface as a
        # phantom key. Refuse, and name the keys the user has to rename.
        logger.warning(
            "ENV REWRITE: {} has an unterminated quoted value; rename {} to "
            "MCC_* by hand (nothing was changed).",
            path,
            ", ".join(old for old, _ in renamed),
        )
        return ()

    backup = _backup_path(path, now)
    try:
        shutil.copy2(path, backup)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("".join(rewritten))
    except OSError as exc:
        logger.warning("ENV REWRITE: cannot rewrite {}: {}", path, exc)
        return ()

    for old_key, new_key in renamed:
        logger.info(
            "ENV REWRITE: renamed {} to {} in {} (value unchanged).",
            old_key,
            new_key,
            path,
        )
    logger.info(
        "ENV REWRITE: {} legacy FCC_* key(s) renamed to MCC_*; the file as it "
        "was is kept at {}.",
        len(renamed),
        backup,
    )
    return tuple(renamed)
