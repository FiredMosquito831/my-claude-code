"""Atomic JSON document writes for user-owned configuration files.

Every file under ``~/.fcc`` that the proxy rewrites is read back by the same
process -- and, for the Claude settings file, by an editor the user may have
open. A partial write is therefore not a cosmetic problem: a truncated document
fails to parse, and the reader has no way to tell "the user emptied this" from
"the writer died halfway".

Staging a sibling ``.fcc-tmp`` and ``os.replace``-ing it is the one mechanism
used for all of them. ``os.replace`` is atomic within a directory on both POSIX
and Windows, and a sibling (rather than the system temp directory) keeps the
rename on the same filesystem, where atomicity is actually guaranteed.
"""

import json
import os
from pathlib import Path

FCC_TEMP_SUFFIX = ".fcc-tmp"


def json_document_bytes(data: object) -> bytes:
    """Return the exact bytes :func:`write_json_document_atomically` would write."""

    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def write_json_document_atomically(path: Path, data: object) -> None:
    """Write ``data`` as JSON to ``path``, creating parent directories as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + FCC_TEMP_SUFFIX)
    try:
        # Bytes, not text: `write_text` translates "\n" to CRLF on Windows, so
        # the same document would differ from itself byte for byte across
        # platforms and defeat the content-compare skip below.
        tmp_path.write_bytes(json_document_bytes(data))
        os.replace(tmp_path, path)
    except OSError:
        # A staged file left behind would be read by nothing, but it would sit
        # next to the real document forever and look like a failed edit.
        tmp_path.unlink(missing_ok=True)
        raise


def write_json_document_atomically_if_changed(path: Path, data: object) -> bool:
    """Write ``data`` only when it differs from what is already on disk.

    Returns whether the file was rewritten. The comparison is not an
    optimisation for its own sake: a generated catalogue is republished on
    every provider refresh, and rewriting an identical file would churn the
    mtime the dashboard shows as "last written" and, on Windows, briefly
    invalidate a handle a running CLI may hold.
    """

    content = json_document_bytes(data)
    try:
        if path.read_bytes() == content:
            return False
    except OSError:
        pass
    write_json_document_atomically(path, data)
    return True
