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


def write_json_document_atomically(path: Path, data: object) -> None:
    """Write ``data`` as JSON to ``path``, creating parent directories as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + FCC_TEMP_SUFFIX)
    content = json.dumps(data, indent=2) + "\n"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        # A staged file left behind would be read by nothing, but it would sit
        # next to the real document forever and look like a failed edit.
        tmp_path.unlink(missing_ok=True)
        raise
