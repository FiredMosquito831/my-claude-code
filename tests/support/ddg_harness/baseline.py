"""Equality baselines: today's output of every method on every fixture, pinned.

A cell is one (method, fixture) pair. Its record holds a readable SHAPE and the
sha256 DIGEST of the outcome's canonical JSON (sorted keys, UTF-8, no
whitespace): the full outcome for search (titles, URLs, snippets, error class
and text, the request the fake upstream saw) and for fetch (url, title, media
type, every character of the extracted text, or the error). A later DDG PR
runs ``moved_cells`` and lists exactly which cells it changes; every cell its
purpose does not name must stay byte-identical.

Regenerate (only on purpose, from a tree whose behaviour is the new base)::

    uv run --offline python -m tests.support.ddg_harness.baseline --write

from a shell whose HOME/USERPROFILE/APPDATA/LOCALAPPDATA/MCC_CONFIG_DIR point
at a scratch directory (WORKING-NOTES II.6 rule 7). Nothing it runs reaches the
network: ``offline.guard`` refuses any non-loopback lookup or connection.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from . import corpus, methods, offline

BASELINE_PATH = corpus.TESTS_DIR / "contracts" / "ddg_harness_baseline.json"

#: Where the cells a DDG PR moves on purpose are recorded, each holding its
#: answer before and after (the two-answer convention of
#: ``test_metadata_ladder_equality.py``). 7.118.2 (DDG-2) opened it: the two
#: ``search:ddgs`` cells that carry results, the live-recorded page's ten D1
#: redirect URLs and the edge page whose scheme-relative ad row leaked. The
#: block is preserved verbatim by ``--write`` -- only the PR that moves a cell
#: again updates it, on purpose -- and ``tests/contracts/
#: test_ddg_harness_equality.py`` pins the moved set to this name.
DDG2_KEY = "moved_by_ddg_2"
DDG2_BEFORE = "v7.118.1"
DDG2_AFTER = "v7.118.2"
#: 7.119.0 (DDG-3) moved twelve ``search:legacy`` cells -- the legacy lite hop
#: re-implemented as declared data: snippets on the three result pages (and
#: the edge page's direct-link row found), the four bot-check pages a
#: ``rate_limit`` naming the check instead of an empty answer, and 403 / 429 /
#: 5xx worded through the shared status mapping. Same convention, its own
#: block, pinned by the DDG-3 tests of ``test_ddg_harness_equality.py``.
DDG3_KEY = "moved_by_ddg_3"
DDG3_BEFORE = "v7.118.2"
DDG3_AFTER = "v7.119.0"
#: 7.119.1 (DDG-3b) moved all sixteen ``search:legacy`` cells again -- the hop
#: completed to R2: the html form asked first (``POST`` with ``q`` and ``b``),
#: the lite page only as the second try. Every cell's answer is 7.119.0's;
#: what moved is the request the fake upstream saw (the html form instead of
#: the lite page, and the lite page after it on the two pages with nothing).
#: Same convention, its own block; ``moved_by_ddg_3`` is kept verbatim as the
#: record of 7.119.0 (its after-answer is this block's before-answer).
DDG3B_KEY = "moved_by_ddg_3b"
DDG3B_BEFORE = "v7.119.0"
DDG3B_AFTER = "v7.119.1"
#: Every moved-cells block, in file order. ``--write`` keeps each verbatim.
MOVED_KEYS = (DDG2_KEY, DDG3_KEY, DDG3B_KEY)


def canonical(outcome: dict[str, Any]) -> bytes:
    return json.dumps(
        outcome, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def digest(outcome: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(outcome)).hexdigest()


def search_shape(outcome: dict[str, Any]) -> dict[str, Any]:
    return {key: outcome[key] for key in sorted(outcome)}


def fetch_shape(outcome: dict[str, Any]) -> dict[str, Any]:
    if outcome["outcome"] != "ok":
        return {key: outcome[key] for key in sorted(outcome)}
    data = str(outcome["data"])
    return {
        "outcome": "ok",
        "url": outcome["url"],
        "title": outcome["title"],
        "media_type": outcome["media_type"],
        "data_chars": len(data),
        "data_sha256": hashlib.sha256(data.encode("utf-8")).hexdigest(),
        "data_head": data[:120],
    }


async def compute_cells() -> dict[str, dict[str, dict[str, Any]]]:
    """Every method over its fixtures -> ``{method: {fixture: {shape, digest}}}``."""

    cells: dict[str, dict[str, dict[str, Any]]] = {}
    for name in methods.SEARCH_METHODS:
        runs = await methods.run_search_method(name)
        cells[f"search:{name}"] = {
            key: {"shape": search_shape(run.outcome), "digest": digest(run.outcome)}
            for key, run in runs.items()
        }
    pages = corpus.all_extraction_pages()
    for name in methods.FETCH_METHODS:
        runs = await methods.run_fetch_method(name, pages)
        cells[f"fetch:{name}"] = {
            key: {"shape": fetch_shape(run.outcome), "digest": digest(run.outcome)}
            for key, run in runs.items()
        }
    return cells


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def moved_cells(
    baseline: dict[str, Any], cells: dict[str, dict[str, dict[str, Any]]]
) -> dict[str, list[str]]:
    """What differs from the baseline: moved digests, missing and added cells."""

    pinned: dict[str, dict[str, dict[str, Any]]] = baseline["cells"]
    moved: list[str] = []
    missing: list[str] = []
    added: list[str] = []
    for method in sorted(set(pinned) | set(cells)):
        old = pinned.get(method, {})
        new = cells.get(method, {})
        for key in sorted(set(old) | set(new)):
            label = f"{method} {key}"
            if key not in new:
                missing.append(label)
            elif key not in old:
                added.append(label)
            elif old[key]["digest"] != new[key]["digest"]:
                moved.append(label)
    return {"moved": moved, "missing": missing, "added": added}


def document(
    cells: dict[str, dict[str, dict[str, Any]]],
    tree: str,
    moved: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The baseline file: ``moved`` maps each ``MOVED_KEYS`` block to its pairs."""

    blocks = moved or {}
    return {
        "about": (
            "Equality baseline of the DDG harness (tests/support/ddg_harness/"
            "baseline.py): today's output of every search and fetch method on "
            "every fixture under tests/fixtures/ddg and tests/fixtures/extraction, "
            "as a readable shape and the sha256 of the canonical outcome JSON. "
            "Pinned by tests/contracts/test_ddg_harness_equality.py."
        ),
        "generated_on": tree,
        **{key: blocks.get(key, {}) for key in MOVED_KEYS},
        "cells": cells,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check (or with --write, regenerate) the DDG harness baseline."
    )
    parser.add_argument("--write", action="store_true", help="rewrite the baseline")
    parser.add_argument(
        "--tree", default="", help="the commit the baseline is generated on"
    )
    parser.add_argument("--out", type=Path, default=BASELINE_PATH)
    args = parser.parse_args(argv)
    with offline.guard():
        cells = methods.run_sync(compute_cells())
    if args.write:
        # The moved-cells records are this file's history, not its output: a
        # regeneration keeps every pair exactly as the release that moved it
        # wrote it (a later PR that moves one again must update it on purpose).
        existing = load_baseline(args.out) if args.out.is_file() else {}
        moved = {key: existing.get(key, {}) for key in MOVED_KEYS}
        text = json.dumps(
            document(cells, args.tree, moved), indent=1, ensure_ascii=False
        )
        args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {args.out} ({sum(len(v) for v in cells.values())} cells)")
        return 0
    diff = moved_cells(load_baseline(args.out), cells)
    for label, rows in diff.items():
        print(f"{label}: {len(rows)}")
        for row in rows:
            print(f"  {row}")
    return 0 if not any(diff.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
