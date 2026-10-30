# DDG harness — DuckDuckGo search and page-extraction measurement

Recorded pages and loopback only: nothing here ever calls DuckDuckGo or any real page
(USER DECISIONS 2026-10-25, Q11). Spec: `specs/RESEARCH-DDG-WEBSEARCH.md` §6 (PR 1 of
DDG-1…6).

## What is where

| Path | What |
|---|---|
| `tests/fixtures/ddg/` | Search pages (two Apache-2.0 octos pages, three MCC-authored synthetic pages), `LICENSE-octos.txt`, and `scenarios.json` (which page or fake body each search scenario serves, with which status or transport failure, and the outcome class the spec asks for). |
| `tests/fixtures/extraction/` | `trafilatura_eval.tar.xz` (106 annotated pages + `evaldata.json`), `python_readability_pages.tar.xz` (181 fixtures with ideal summaries), each with a `*.SOURCE.txt` sidecar (origin, commit, licence, archive and member sha256), and `mcc_fetch_scenarios.json` (response shapes no recorded page covers). |
| `corpus.py` | Loads the fixtures, checks every archive hash, parses `SOURCE` lines. |
| `methods.py` | MCC's real code over the fixtures: `ddgs` (the provider with `DDGS_BACKEND=duckduckgo`, real `ddgs` package, mock HTTP transport), `legacy` (`outbound._legacy_route_scrape`: the lite-page hop read by the declared page profiles of 7.119.0, as the route with no chain set runs it; mock transport), `web_fetch` (`outbound._run_web_fetch` against a loopback aiohttp server). |
| `scoring.py` | Search and extraction scores. |
| `baseline.py` | Equality baselines (`tests/contracts/ddg_harness_baseline.json`). |
| `table.py` | The before/after table. |
| `first_use.py`, `first_use_probe.py` | The fresh-interpreter timing of the first `web_fetch`. |
| `offline.py` | The scripts' own network guard (pytest has the suite's gate). |

Every fixture file carries a `SOURCE` line (`SOURCE: origin=…; licence=…; fetched=…; what=…`),
pinned by `tests/contracts/test_ddg_fixture_sources.py`: the licence must be permissive
(Apache-2.0, MIT, BSD, CC-BY-4.0) or `MCC-own`; nothing from an AGPL/GPL/FSL project, and
never SearXNG, Crush, Firecrawl or html2text (Q13). A copied page must still be the upstream
bytes its `SOURCE` line hashes. The fixture directories are `-text` in `.gitattributes`, so a
CRLF checkout serves the same bytes as CI.

## Commands

Run from a shell whose `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA` (and `MCC_CONFIG_DIR`)
point at a scratch directory (WORKING-NOTES II.6 rule 7):

```
uv run --offline python -m tests.support.ddg_harness.table                      # legacy vs ddgs, today's extractor
uv run --offline python -m tests.support.ddg_harness.table --before legacy --after <new>
uv run --offline python -m tests.support.ddg_harness.table --before web_fetch --after <new> --first-use
uv run --offline python -m tests.support.ddg_harness.baseline                   # list moved cells
uv run --offline python -m tests.support.ddg_harness.baseline --write --tree <sha>   # only on purpose
```

## For the next DDG PRs

* Register a new method in `methods.SEARCH_METHODS` / `methods.FETCH_METHODS` to get a column
  in the table; a method that replaces today's code keeps today's name.
* `python -m tests.support.ddg_harness.baseline` lists exactly the cells a change moves. A PR
  names the cells its purpose changes and proves every other cell byte-identical; then it
  regenerates the baseline on its own tree and records each moved cell's two answers under
  its own `moved_by_ddg_<n>` block (DDG-2: `moved_by_ddg_2`, 2 `ddgs` cells; DDG-3:
  `moved_by_ddg_3`, 12 `legacy` cells). `--write` keeps every such block verbatim.
* The baseline pins today's defects as they are (D1–D3 of the spec, and `web_fetch` failing on
  a response with no charset parameter); `test_the_baseline_records_todays_findings_as_they_are`
  names them.
* A new fixture gets a `SOURCE` line and an entry in `scenarios.json` (search) — the
  contracts fail otherwise.
