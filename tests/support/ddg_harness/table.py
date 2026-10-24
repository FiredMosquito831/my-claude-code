"""Print the before/after table for any two harness methods (Markdown).

    uv run --offline python -m tests.support.ddg_harness.table
    uv run --offline python -m tests.support.ddg_harness.table --before legacy --after ddgs
    uv run --offline python -m tests.support.ddg_harness.table --before web_fetch --after web_fetch

Search methods (``methods.SEARCH_METHODS``) are scored on the search scenarios,
fetch methods (``methods.FETCH_METHODS``) on both extraction corpora; with no
arguments the table compares today's two search paths and prints today's
extractor alone. ``--first-use`` adds the first-use timing probe. Offline only
(``offline.guard``); run it from a shell with a redirected home (WORKING-NOTES
II.6 rule 7).
"""

import argparse
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import corpus, first_use, methods, offline, scoring

DESCRIPTION = "Print the DDG harness before/after table for two methods."


def _ratio(part: int, whole: int) -> str:
    return f"{part}/{whole}" if whole else f"{part}/0"


def _search_rows(summary: scoring.SearchSummary) -> dict[str, str]:
    return {
        "results returned (of real results on result pages)": _ratio(
            summary.returned, summary.real_results
        ),
        "destination URL correct (of returned)": _ratio(
            summary.url_ok, summary.returned
        ),
        "title non-empty (of returned)": _ratio(summary.titles, summary.returned),
        "snippet non-empty (of returned)": _ratio(summary.snippets, summary.returned),
        "ads leaked into results": str(summary.ads_leaked),
        "challenge detected (of challenge scenarios)": _ratio(
            summary.challenge_detected, summary.challenge_pages
        ),
        "challenge verdict right (of all scenarios)": _ratio(
            summary.challenge_ok, summary.scenarios
        ),
        "outcome class as specified (of all scenarios)": _ratio(
            summary.class_ok, summary.scenarios
        ),
        "median ms (in-process, fake transport)": f"{summary.median_ms:.1f}",
    }


def _extraction_rows(summary: scoring.ExtractionSummary) -> dict[str, str]:
    return {
        "pages": str(summary.pages),
        "precision": f"{summary.precision:.3f}",
        "recall": f"{summary.recall:.3f}",
        "F1": f"{summary.f1:.3f}",
        "median ms (loopback fetch, end to end)": f"{summary.median_ms:.1f}",
        "p95 ms": f"{summary.p95_ms:.1f}",
        "median output chars": str(summary.median_chars),
        f"pages under {scoring.UNDER_CHARS} chars": str(summary.under_200_chars),
        "failures": str(summary.failures),
    }


def _two_columns(
    title: str,
    before: str,
    after: str,
    rows_before: dict[str, str],
    rows_after: dict[str, str],
) -> list[str]:
    lines = [f"### {title}", "", f"| metric | {before} | {after} |", "|---|---|---|"]
    lines += [
        f"| {metric} | {value} | {rows_after[metric]} |"
        for metric, value in rows_before.items()
    ]
    return [*lines, ""]


async def search_cells(name: str) -> list[scoring.SearchCell]:
    runs = await methods.run_search_method(name)
    return [
        scoring.score_search_cell(scenario, runs[scenario.id])
        for scenario in corpus.search_corpus().scenarios
    ]


def _cell_text(cell: scoring.SearchCell) -> str:
    text = f"{cell.observed} n={cell.returned}"
    if cell.returned:
        text += f" url_ok={cell.url_ok} snip={cell.snippets}"
    if cell.ads:
        text += f" ads={cell.ads}"
    return text + ("" if cell.class_ok else " (x)")


async def search_table(before: str, after: str) -> list[str]:
    cells_before = await search_cells(before)
    cells_after = await search_cells(after)
    lines = _two_columns(
        "Search (tests/fixtures/ddg, 0 live calls)",
        before,
        after,
        _search_rows(scoring.summarise_search(cells_before)),
        _search_rows(scoring.summarise_search(cells_after)),
    )
    lines += [
        "| scenario | specified | " + before + " | " + after + " |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {old.scenario} | {old.expect} | {_cell_text(old)} | {_cell_text(new)} |"
        for old, new in zip(cells_before, cells_after, strict=True)
    ]
    lines += [
        "",
        "(x) = outcome class differs from the one scenarios.json specifies.",
        "",
    ]
    return lines


def _extraction_corpora() -> dict[str, tuple[corpus.ExtractionPage, ...]]:
    return {
        "trafilatura evaluation set (with/without strings)": corpus.trafilatura_pages(),
        "python-readability fixtures (ideal-summary tokens)": corpus.readability_pages(),
    }


async def extraction_summaries(name: str) -> list[scoring.ExtractionSummary]:
    summaries = []
    for label, pages in _extraction_corpora().items():
        runs = await methods.run_fetch_method(name, pages)
        summaries.append(scoring.summarise_extraction(label, pages, runs))
    return summaries


async def extraction_table(before: str, after: str) -> list[str]:
    lines: list[str] = []
    old = await extraction_summaries(before)
    new = old if after == before else await extraction_summaries(after)
    for summary_before, summary_after in zip(old, new, strict=True):
        lines += _two_columns(
            f"Extraction: {summary_before.corpus}",
            before,
            after,
            _extraction_rows(summary_before),
            _extraction_rows(summary_after),
        )
    return lines


def _kind(name: str) -> str:
    if name in methods.SEARCH_METHODS:
        return "search"
    if name in methods.FETCH_METHODS:
        return "fetch"
    known = sorted([*methods.SEARCH_METHODS, *methods.FETCH_METHODS])
    raise SystemExit(f"unknown method {name!r}; known: {', '.join(known)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--first-use", action="store_true")
    args = parser.parse_args(argv)
    lines: list[str] = []
    jobs: list[Callable[[], Any]] = []
    if args.before is None and args.after is None:
        jobs = [
            lambda: search_table("legacy", "ddgs"),
            lambda: extraction_table("web_fetch", "web_fetch"),
        ]
    else:
        before = args.before or args.after
        after = args.after or args.before
        if _kind(before) != _kind(after):
            raise SystemExit(
                "--before and --after must both be search or both be fetch methods"
            )
        table = search_table if _kind(before) == "search" else extraction_table
        jobs = [lambda: table(before, after)]
    with offline.guard():
        for job in jobs:
            lines += methods.run_sync(job())
    if args.first_use:
        probe = first_use.run_probe(Path(tempfile.mkdtemp(prefix="mcc-ddg-first-use-")))
        lines += ["### First use of the extractor path (fresh interpreter)", ""]
        lines += [f"- {key}: {value}" for key, value in probe.items()]
        lines.append("")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
