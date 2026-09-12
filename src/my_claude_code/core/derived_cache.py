"""Derived payloads that survive a restart, keyed on the data that made them.

The dashboard recomputes everything expensive on every start and, for anything
not in the five-second in-memory cache, on every page open as well. The request
log's own rollup tables (6.17.0) and the learned-fact store (6.52.0) are the two
places in this codebase where a derivation is already durable, and they are the
two places this module copies.

**The key is never a clock.** A cached payload is valid exactly as long as the
data that produced it has not changed, and a wall-clock TTL answers a different
question: it expires a payload nothing changed, and it serves a payload
something did. Every entry therefore carries a *key* built from markers of the
inputs -- for the request log, the highest rowid it had plus the migration
markers -- and a mismatch is the only thing that invalidates it.

What lives here is deliberately small: read a document, write a document, and
say whether the key still matches. Which payloads are stored, and what their
keys are made of, belongs to the callers that know their inputs.

Import boundary: ``core`` may not import ``config``, so the cache root arrives
as a path rather than being resolved here. That is also what lets a test point
it at a temporary directory without touching a real home.
"""

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Bumped when the *envelope* changes shape. A document written by an older
#: version is ignored rather than migrated: it is a cache, and recomputing is
#: always available.
DERIVED_CACHE_VERSION = 1

_TEMP_SUFFIX = ".mcc-tmp"


@dataclass(frozen=True, slots=True)
class DerivedEntry:
    """One stored payload, with the key it was computed from and when."""

    key: str
    computed_at: float
    payload: Any

    def matches(self, key: str) -> bool:
        return self.key == key


class DerivedCache:
    """A directory of ``{version, key, computed_at, payload}`` JSON documents.

    Every operation is best-effort: a cache that cannot be read, written or
    parsed must never be the reason a page fails to render. Failures are
    reported by returning ``None`` (read) or ``False`` (write), never raised.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, name: str) -> Path:
        """Where ``name`` is stored. One flat directory, no nesting.

        ``name`` is a caller-chosen identifier, not user input; it is still
        checked, because a name with a separator in it would write outside the
        cache root and that is exactly the kind of thing a later caller does by
        accident.
        """

        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"Not a derived-cache entry name: {name!r}")
        return self._root / f"{name}.json"

    def read(self, name: str) -> DerivedEntry | None:
        """Return the stored entry, or ``None`` if there is not a usable one."""

        try:
            raw = self.path_for(name).read_bytes()
        except OSError, ValueError:
            return None
        try:
            document = json.loads(raw)
        except ValueError, UnicodeDecodeError:
            # A half-written or hand-edited file is a cache miss, not a fault.
            return None
        if not isinstance(document, dict):
            return None
        if document.get("version") != DERIVED_CACHE_VERSION:
            return None
        key = document.get("key")
        computed_at = document.get("computed_at")
        if not isinstance(key, str) or not isinstance(computed_at, (int, float)):
            return None
        if "payload" not in document:
            return None
        return DerivedEntry(
            key=key, computed_at=float(computed_at), payload=document["payload"]
        )

    def write(self, name: str, *, key: str, payload: Any, computed_at: float) -> bool:
        """Store ``payload`` under ``name``. Returns whether it landed.

        Atomic: written to a temporary file in the same directory and renamed,
        so a reader never sees half a document and a crash never leaves one.
        """

        try:
            path = self.path_for(name)
        except ValueError:
            return False
        document = {
            "version": DERIVED_CACHE_VERSION,
            "key": key,
            "computed_at": float(computed_at),
            "payload": payload,
        }
        try:
            content = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        except TypeError, ValueError:
            # A payload that cannot be serialised is a programming error in the
            # caller, but it must not take the page down with it.
            return False
        with self._lock:
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                handle, temp_name = tempfile.mkstemp(
                    dir=self._root, suffix=_TEMP_SUFFIX
                )
                try:
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(content)
                    os.replace(temp_name, path)
                except BaseException:
                    Path(temp_name).unlink(missing_ok=True)
                    raise
            except OSError:
                return False
        return True

    def forget(self, name: str) -> None:
        """Remove one entry. Missing is success."""

        with contextlib.suppress(OSError, ValueError):
            self.path_for(name).unlink(missing_ok=True)
