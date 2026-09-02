"""File-first access to the harness documents the server maintains.

A launcher used to fetch ``GET /admin/api/catalogue-models`` on **every**
launch, give it the ``/health`` preflight's 1.5 s budget, and write the document
it was handed. Three things were wrong with that at once, and together they
produced a failure that could never heal:

* the budget was sized for a route that answers in milliseconds, while the one
  being called serialises every registered harness's document from the
  resolution ladder and measured **1.8-4.0 s** on a real 292-model install;
* the launcher was the **only** thing that could create the file, because the
  server's fan-out publisher refreshed a document only where one already
  existed;
* the fallback for a failed fetch was "launch without an MCC provider", which
  leaves the file still absent.

So the first launch failed, and so did every launch after it, with
``could not prepare the OpenCode config (timed out)`` and no MCC models in the
picker -- permanently.

The server now materialises every harness's document at startup and rewrites it
on every publish (``runtime/harness_catalogues.py``), which makes the file the
authority and the fetch a cold-start path. This module is what the launchers
read it through:

* :func:`document_on_disk` -- is there a usable document at this path? A file
  that will not parse answers no, so a truncated write falls back to a fetch
  rather than handing a CLI something it will reject.
* :func:`warn_catalogue_unavailable` -- when the cold-start fetch fails, say
  which file was missing, what was tried, and what to do about it. The old
  warning named the symptom (``timed out``) and stopped there.
"""

import json
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from my_claude_code.cli.harnesses.catalogue_client import (
    CATALOGUE_MODELS_PATH,
    catalogue_fetch_timeout,
)


def read_document(
    path: Path, document_format: str = "json"
) -> Mapping[str, Any] | None:
    """Return the generated document at *path*, or ``None`` when unusable.

    ``None`` for a file that is absent, unreadable or unparseable, because the
    caller's next move is the same for all three: build it from the server.
    ``tomllib`` reads and does not write, which is the half needed here.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        if document_format == "toml":
            payload: Any = tomllib.loads(raw.decode("utf-8"))
        else:
            payload = json.loads(raw.decode("utf-8"))
    except ValueError, tomllib.TOMLDecodeError, UnicodeDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def document_on_disk(path: Path, document_format: str = "json") -> bool:
    """Return whether a usable generated document already sits at *path*."""

    return read_document(path, document_format) is not None


def warn_catalogue_unavailable(
    *,
    display_name: str,
    launcher_command: str,
    path: Path | None,
    proxy_root_url: str,
    exc: BaseException,
    consequence: str = "Launching without an MCC provider.",
) -> None:
    """Say what was missing, what was tried, and what the user can do.

    Three sentences on purpose. The old one line said ``could not prepare the
    OpenCode config (timed out)``, which names neither the file that would have
    fixed it nor the request that failed nor anything to try -- and the user who
    reported this had no way to tell from it that the condition was permanent.
    """

    where = f" at {path}" if path is not None else ""
    print(
        f"My Claude Code warning: no {display_name} model list{where}, and "
        f"building one failed: {exc}.",
        file=sys.stderr,
    )
    print(
        f"  Tried GET {proxy_root_url.rstrip('/')}{CATALOGUE_MODELS_PATH} with a "
        f"{catalogue_fetch_timeout():g}s budget (CATALOGUE_FETCH_TIMEOUT_SECONDS).",
        file=sys.stderr,
    )
    print(
        f"  Start the server with mcc-server, or run {launcher_command} again "
        "once the dashboard's Coding agents page shows this agent's catalogue. "
        f"{consequence}",
        file=sys.stderr,
    )
