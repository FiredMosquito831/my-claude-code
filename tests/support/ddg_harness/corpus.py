"""The harness's recorded pages: where they live, what they are, and their provenance.

Two fixture directories, each file carrying a ``SOURCE`` line:

* ``tests/fixtures/ddg`` -- DuckDuckGo results and challenge pages (the two
  Apache-2.0 octos pages, three MCC-authored synthetic pages) and
  ``scenarios.json``, which says which page or fake body each search scenario
  serves with which status, and the outcome class the spec asks for.
* ``tests/fixtures/extraction`` -- the two annotated extraction corpora, stored
  xz-compressed with a ``<archive>.SOURCE.txt`` sidecar that holds the archive's
  sha256 and every member's (checked on load: a damaged copy fails loudly), and
  ``mcc_fetch_scenarios.json`` (response shapes no recorded page covers).

A text fixture's ``SOURCE`` line is its first line (an HTML comment for a page,
the ``"SOURCE"`` key of a JSON document, the first line of a licence copy):
``SOURCE: origin=...; licence=...; fetched=...; what=...`` -- see
``source_fields``. Standard library only; nothing here touches the network.
"""

import base64
import codecs
import functools
import hashlib
import io
import json
import lzma
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parents[2]
DDG_DIR = TESTS_DIR / "fixtures" / "ddg"
EXTRACTION_DIR = TESTS_DIR / "fixtures" / "extraction"
FIXTURE_DIRS = (DDG_DIR, EXTRACTION_DIR)

TRAFILATURA_ARCHIVE = EXTRACTION_DIR / "trafilatura_eval.tar.xz"
READABILITY_ARCHIVE = EXTRACTION_DIR / "python_readability_pages.tar.xz"
ARCHIVE_SUFFIX = ".tar.xz"
SIDECAR_SUFFIX = ".SOURCE.txt"

#: Licences a fixture may carry: permissive ones, and MCC's own for the files
#: authored in this repository. The dual licence (AGPL-3.0-or-later OR
#: commercial) forbids copying AGPL/GPL/FSL material into the commercial half
#: (USER DECISIONS 2026-10-25 Q13), so a fixture's licence must be on this list.
ALLOWED_LICENCES = frozenset(
    {"Apache-2.0", "MIT", "BSD-2-Clause", "BSD-3-Clause", "CC-BY-4.0", "MCC-own"}
)
#: Licence words that must never appear in a fixture's licence field.
DENIED_LICENCE_PATTERN = re.compile(r"AGPL|LGPL|\bGPL|FSL|SSPL|BUSL", re.IGNORECASE)
#: Projects whose code and fixtures are never copied (Q13), matched in ``origin``.
DENIED_ORIGINS = (
    "searxng",
    "charmbracelet",
    "crush",
    "firecrawl",
    "mendableai",
    "html2text",
    "sei-studio",
)

_SOURCE_PREFIX = "SOURCE:"
_FIELD = re.compile(r"^([a-z][a-z0-9_]*)=")


def source_fields(line: str) -> dict[str, str]:
    """Parse ``SOURCE: key=value; key=value ...`` into a dict.

    A segment that does not start with ``key=`` belongs to the previous value
    (``what=`` prose may itself contain ``"; "``).
    """

    text = line.strip()
    if text.startswith("<!--"):
        text = text.removeprefix("<!--").removesuffix("-->").strip()
    text = text.removeprefix(_SOURCE_PREFIX).strip()
    fields: dict[str, str] = {}
    last: str | None = None
    for segment in text.split("; "):
        match = _FIELD.match(segment)
        if match:
            last = match.group(1)
            fields[last] = segment[match.end() :].strip()
        elif last is not None:
            fields[last] = f"{fields[last]}; {segment}"
    return fields


def source_line_of(path: Path) -> str | None:
    """The ``SOURCE`` line a fixture file carries, or ``None``.

    An archive's line is the first line of its sidecar; a JSON document's is its
    ``"SOURCE"`` key; any other text file's is its first line.
    """

    if path.name.endswith(ARCHIVE_SUFFIX):
        sidecar = path.with_name(path.name + SIDECAR_SUFFIX)
        if not sidecar.is_file():
            return None
        path = sidecar
    raw = path.read_bytes()
    if path.suffix == ".json":
        document = json.loads(raw.decode("utf-8"))
        value = document.get("SOURCE") if isinstance(document, dict) else None
        return f"{_SOURCE_PREFIX} {value}" if isinstance(value, str) else None
    first = raw.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    return first if _SOURCE_PREFIX in first else None


def body_after_source_line(path: Path) -> bytes:
    """A recorded page's bytes as upstream published them (the SOURCE line removed)."""

    raw = path.read_bytes()
    first, _, rest = raw.partition(b"\n")
    if _SOURCE_PREFIX.encode() not in first:
        raise ValueError(f"{path} has no SOURCE line")
    return rest


def fixture_files() -> list[Path]:
    """Every file under the two fixture directories (sidecars included)."""

    return sorted(
        path for root in FIXTURE_DIRS for path in root.rglob("*") if path.is_file()
    )


# --------------------------------------------------------------------------- #
# Archives


def _sidecar_value(sidecar_text: str, key: str) -> str:
    for line in sidecar_text.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    raise ValueError(f"sidecar has no {key} line")


def _sidecar_manifest(sidecar_text: str) -> dict[str, str]:
    manifest: dict[str, str] = {}
    inside = False
    for line in sidecar_text.splitlines():
        if line.startswith("MANIFEST"):
            inside = True
            continue
        if inside and line.startswith("  "):
            digest, name = line.strip().split("  ", 1)
            manifest[name] = digest
    return manifest


@functools.cache
def archive_members(archive: Path) -> dict[str, bytes]:
    """The archive's members by name, after every hash in its sidecar checks out."""

    sidecar_text = archive.with_name(archive.name + SIDECAR_SUFFIX).read_text(
        encoding="utf-8"
    )
    packed = archive.read_bytes()
    expected = _sidecar_value(sidecar_text, "ARCHIVE_SHA256")
    if hashlib.sha256(packed).hexdigest() != expected:
        raise RuntimeError(f"{archive} does not match the sha256 in its sidecar")
    tar_bytes = lzma.decompress(packed)
    if hashlib.sha256(tar_bytes).hexdigest() != _sidecar_value(
        sidecar_text, "TAR_SHA256"
    ):
        raise RuntimeError(f"{archive} unpacks to a tar its sidecar does not name")
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
        for info in tar.getmembers():
            if not info.isfile():
                continue
            handle = tar.extractfile(info)
            if handle is None:
                continue
            members[info.name] = handle.read()
    manifest = _sidecar_manifest(sidecar_text)
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}
    if digests != manifest or len(members) != int(
        _sidecar_value(sidecar_text, "MEMBERS")
    ):
        raise RuntimeError(f"{archive} members do not match its sidecar manifest")
    return members


# --------------------------------------------------------------------------- #
# Search scenarios


@dataclass(frozen=True, slots=True)
class SearchPage:
    name: str
    body: bytes
    real_results: int
    challenge: bool
    ad_destination_hosts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchScenario:
    """One response the fake upstream gives, whatever endpoint is asked."""

    id: str
    expect: str
    status: int | None = None
    body: bytes = b""
    page: SearchPage | None = None
    transport: str | None = None  # "timeout" | "refused"


@dataclass(frozen=True, slots=True)
class SearchCorpus:
    query: str
    max_results: int
    pages: tuple[SearchPage, ...]
    scenarios: tuple[SearchScenario, ...]


@functools.cache
def search_corpus() -> SearchCorpus:
    document: dict[str, Any] = json.loads(
        (DDG_DIR / "scenarios.json").read_text(encoding="utf-8")
    )
    pages = {
        name: SearchPage(
            name=name,
            body=body_after_source_line(DDG_DIR / name),
            real_results=int(meta["real_results"]),
            challenge=bool(meta["challenge"]),
            ad_destination_hosts=tuple(meta["ad_destination_hosts"]),
        )
        for name, meta in document["pages"].items()
    }
    scenarios = []
    for row in document["scenarios"]:
        page = pages[row["page"]] if "page" in row else None
        scenarios.append(
            SearchScenario(
                id=row["id"],
                expect=row["expect"],
                status=row.get("status"),
                body=page.body if page else str(row.get("body", "")).encode("utf-8"),
                page=page,
                transport=row.get("transport"),
            )
        )
    return SearchCorpus(
        query=document["query"],
        max_results=int(document["max_results"]),
        pages=tuple(pages.values()),
        scenarios=tuple(scenarios),
    )


# --------------------------------------------------------------------------- #
# Extraction pages

_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE
)


def declared_charset(raw: bytes) -> str:
    """The charset a page declares (BOM, then ``<meta>``), else ``utf-8``.

    The recorded pages come without their original HTTP headers, so the
    loopback server needs a ``Content-Type`` charset to send; it sends the one
    the page itself declares, as a typical origin server does. (A response
    with NO charset fails today's ``web_fetch`` outright -- see the
    ``html_without_charset`` fetch scenario.)
    """

    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    match = _META_CHARSET.search(raw[:4096])
    if match:
        name = match.group(1).decode("ascii", errors="ignore")
        try:
            codecs.lookup(name)
        except LookupError:
            return "utf-8"
        return name
    return "utf-8"


@dataclass(frozen=True, slots=True)
class ExtractionPage:
    """One response the loopback fetch server gives, and how to score it."""

    corpus: str  # "trafilatura" | "readability" | "ddg_pages" | "fetch_scenarios"
    name: str
    body: bytes
    content_type: str
    status: int = 200
    headers: tuple[tuple[str, str], ...] = ()
    with_strings: tuple[str, ...] = ()
    without_strings: tuple[str, ...] = ()
    ideal_html: str | None = None

    @property
    def key(self) -> str:
        return f"{self.corpus}/{self.name}"


def _html_page(corpus: str, name: str, raw: bytes, **extra: Any) -> ExtractionPage:
    return ExtractionPage(
        corpus=corpus,
        name=name,
        body=raw,
        content_type=f"text/html; charset={declared_charset(raw)}",
        **extra,
    )


@functools.cache
def trafilatura_pages() -> tuple[ExtractionPage, ...]:
    """The 106 annotated pages of trafilatura's evaluation set (with/without strings)."""

    members = archive_members(TRAFILATURA_ARCHIVE)
    evaldata: dict[str, Any] = json.loads(members["tests/evaldata.json"])
    pages: dict[str, ExtractionPage] = {}
    for item in evaldata.values():
        name = item["file"]
        raw = members.get(f"tests/cache/{name}")
        if raw is None or name in pages:
            continue
        pages[name] = _html_page(
            "trafilatura",
            name,
            raw,
            with_strings=tuple(item.get("with", [])),
            without_strings=tuple(item.get("without", [])),
        )
    return tuple(pages[name] for name in sorted(pages))


@functools.cache
def readability_pages() -> tuple[ExtractionPage, ...]:
    """python-readability's 181 benchmark fixtures (page + ideal summary)."""

    members = archive_members(READABILITY_ARCHIVE)
    names = sorted(
        name.removesuffix("/page.html")
        for name in members
        if name.startswith("pages/") and name.endswith("/page.html")
    )
    return tuple(
        _html_page(
            "readability",
            name.removeprefix("pages/"),
            members[f"{name}/page.html"],
            ideal_html=members[f"{name}/ideal.html"].decode("utf-8", errors="replace"),
        )
        for name in names
    )


@functools.cache
def ddg_page_fetches() -> tuple[ExtractionPage, ...]:
    """The search pages, fetched as pages (what ``web_fetch`` makes of them)."""

    return tuple(
        _html_page("ddg_pages", page.name, page.body) for page in search_corpus().pages
    )


@functools.cache
def fetch_scenarios() -> tuple[ExtractionPage, ...]:
    document: dict[str, Any] = json.loads(
        (EXTRACTION_DIR / "mcc_fetch_scenarios.json").read_text(encoding="utf-8")
    )
    rows = []
    for row in document["scenarios"]:
        body = (
            base64.b64decode(row["body_base64"])
            if "body_base64" in row
            else str(row.get("body", "")).encode("utf-8")
        )
        rows.append(
            ExtractionPage(
                corpus="fetch_scenarios",
                name=row["id"],
                body=body,
                content_type=row["content_type"],
                status=int(row["status"]),
                headers=tuple(sorted(dict(row.get("headers", {})).items())),
            )
        )
    return tuple(rows)


def all_extraction_pages() -> tuple[ExtractionPage, ...]:
    return (
        *fetch_scenarios(),
        *ddg_page_fetches(),
        *trafilatura_pages(),
        *readability_pages(),
    )
