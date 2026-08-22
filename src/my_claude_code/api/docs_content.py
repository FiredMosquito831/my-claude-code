"""The project's user-facing documentation, as shipped inside the package.

The dashboard renders these documents itself instead of linking out to
GitHub. Three things follow from that choice and none of them are
incidental:

* the Docs page works with no internet at all;
* it opens instantly, with no request to a third party and nothing about
  the reader leaving the machine;
* it shows the documentation for the version that is actually running. A
  user on 5.40 reading 5.48's instructions is worse than no documentation,
  because it looks authoritative.

Each document still carries a "View on GitHub" link for whoever wants the
latest instead of the installed one.

The list is curated to what someone *running* MCC needs. `docs/AGENT_SPEC_*`,
`docs/RELEASE-CHECKLIST.md`, `docs/BRAND.md`, `docs/adr/*`, `research/*` and
`smoke/README.md` are written for whoever builds MCC and are deliberately
absent.

Where the files live at runtime
-------------------------------
`pyproject.toml` force-includes each document into `my_claude_code/docs_bundle/`
in the wheel, the same way `.env.example` is force-included into
`my_claude_code/config/env.example`. That directory therefore exists in an
installed wheel and does *not* exist in a source checkout, where the package is
installed editable and hatchling's force-include never runs. `_bundle_dir()`
falls back to the repo checkout so a developer sees the same page, and
`tests/api/test_docs_bundle_wheel.py` builds a real wheel and asserts every
document is inside it -- without that test the fallback would mask a page that
is empty for every installed user while working perfectly in development.
"""

from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

# Every "View on GitHub" link and every rewritten relative link points at the
# repository's default branch: the reader is on this page precisely because
# they want the installed version, so the outbound link is only useful if it
# offers the other thing -- the latest.
RELEASE_REPO = "FiredMosquito831/my-claude-code"
GITHUB_BLOB_BASE = f"https://github.com/{RELEASE_REPO}/blob/main"


class Document(NamedTuple):
    """One curated document.

    ``slug`` is the URL segment and the only value ever accepted from a
    client. ``repo_path`` is where the file lives in the repository, used
    for the GitHub link and for the source-checkout fallback.
    ``bundled_name`` is its flat name inside ``docs_bundle/``.
    """

    slug: str
    title: str
    summary: str
    repo_path: str

    @property
    def bundled_name(self) -> str:
        return self.repo_path.rsplit("/", 1)[-1]

    @property
    def github_url(self) -> str:
        return f"{GITHUB_BLOB_BASE}/{self.repo_path}"


# Order is the reading order the page presents, not alphabetical: what the
# project is, then how to use it, then how it is built.
DOCUMENTS: tuple[Document, ...] = (
    Document(
        slug="readme",
        title="README",
        summary="What MCC is, what it connects to, and how to install it.",
        repo_path="README.md",
    ),
    Document(
        slug="usage",
        title="Usage",
        summary="Running the server day to day, and every setting it reads.",
        repo_path="docs/USAGE.md",
    ),
    Document(
        slug="claude-code-config",
        title="Claude Code Config",
        summary="Pointing Claude Code at this proxy, per session or permanently.",
        repo_path="docs/CLAUDE-CODE-CONFIG.md",
    ),
    Document(
        slug="anthropic-subscription",
        title="Anthropic Subscription",
        summary="Using an Anthropic Console key, and why subscription OAuth is not offered.",
        repo_path="docs/ANTHROPIC-SUBSCRIPTION.md",
    ),
    Document(
        slug="architecture",
        title="Architecture",
        summary="How a request travels through the proxy, and who owns what.",
        repo_path="ARCHITECTURE.md",
    ),
    Document(
        slug="contributing",
        title="Contributing",
        summary="Local checks, versioning rules, and how changes get merged.",
        repo_path="CONTRIBUTING.md",
    ),
)

DOCUMENT_BY_SLUG: dict[str, Document] = {doc.slug: doc for doc in DOCUMENTS}

# Relative link targets that resolve to another curated document, so a
# cross-reference inside the prose stays inside the dashboard instead of
# throwing the reader onto GitHub mid-sentence.
_SLUG_BY_REPO_PATH: dict[str, str] = {doc.repo_path: doc.slug for doc in DOCUMENTS}


@lru_cache(maxsize=1)
def _bundle_dir() -> Path | None:
    """Directory holding the bundled documents, or ``None`` if there is none.

    Prefers the wheel's ``docs_bundle/``. Falls back to the repository
    checkout so the page is not blank while developing -- see the module
    docstring for why that fallback needs a wheel test to stay honest.
    """

    bundled = Path(__file__).resolve().parent.parent / "docs_bundle"
    if bundled.is_dir():
        return bundled

    # src/my_claude_code/api/docs_content.py -> repository root
    checkout = Path(__file__).resolve().parents[3]
    if (checkout / "README.md").is_file():
        return checkout
    return None


def _document_path(document: Document) -> Path | None:
    """Resolve one document to a real file, or ``None``.

    In a wheel the documents are flat; in a checkout they sit at their
    repository paths. Both are looked up from the curated table, never
    built out of anything a client sent.
    """

    directory = _bundle_dir()
    if directory is None:
        return None
    for candidate in (
        directory / document.bundled_name,
        directory / document.repo_path,
    ):
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def available_slugs() -> frozenset[str]:
    """Slugs whose file is actually present.

    A slug is matched against this set rather than being joined onto a
    directory, which makes path traversal impossible by construction
    instead of by sanitising -- the same shape as ``_bundled_image_names()``
    for the guide screenshots.
    """

    return frozenset(doc.slug for doc in DOCUMENTS if _document_path(doc) is not None)


def available_documents() -> tuple[Document, ...]:
    present = available_slugs()
    return tuple(doc for doc in DOCUMENTS if doc.slug in present)


@lru_cache(maxsize=len(DOCUMENTS))
def load_markdown(slug: str) -> str | None:
    """Markdown source for ``slug``, or ``None`` if it is not a curated document.

    ``slug`` is only ever used as a dictionary key.
    """

    if slug not in available_slugs():
        return None
    path = _document_path(DOCUMENT_BY_SLUG[slug])
    if path is None:
        return None
    return path.read_text(encoding="utf-8")


def resolve_relative_link(href: str) -> str:
    """Rewrite a repository-relative link for the dashboard.

    A link to another curated document becomes an in-page link, so a
    cross-reference does not eject the reader to a browser tab. Everything
    else -- source files, `.env.example`, directories -- becomes a GitHub
    link, because the dashboard has nothing to show for it.
    """

    target = href.split("#", 1)[0].split("?", 1)[0].lstrip("./")
    anchor = href[len(href.split("#", 1)[0]) :]
    slug = _SLUG_BY_REPO_PATH.get(target)
    if slug is not None:
        return f"#doc-{slug}{anchor}"
    return f"{GITHUB_BLOB_BASE}/{target}{anchor}" if target else href
