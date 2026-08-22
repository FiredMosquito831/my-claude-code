"""Render a bundled document to the HTML the dashboard shows.

Renderer choice
---------------
`markdown-it-py` is already a runtime dependency -- `messaging/rendering/`
uses it to turn assistant replies into Telegram and Discord markup -- so the
Docs page adds no dependency at all. It is a CommonMark-compliant parser
rather than a pile of regexes, which matters for a 1,199-line README full of
fenced blocks, tables and nested lists.

Raw HTML in the source
----------------------
Disabled: the parser is built with ``{"html": False}``, exactly as the two
existing renderers are. Raw HTML in a document is therefore escaped and
displayed as literal text, never injected into the page. These documents are
trusted content today; a renderer that passes raw HTML through is a footgun
the moment anything else -- a plugin, a fetched doc, a user-supplied file --
is fed to it, and the failure mode is script injection into a page that is
already authenticated to a local admin API.

Everything the page emits is produced here, on the server, and the browser
only ever assigns it. No markdown is parsed in JavaScript.
"""

import re
from functools import lru_cache
from typing import NamedTuple

from markdown_it import MarkdownIt
from markdown_it.token import Token

from my_claude_code.api.docs_content import (
    DOCUMENT_BY_SLUG,
    Document,
    load_markdown,
    resolve_relative_link,
)

# Headings deep enough to be worth linking to. h1 is the document's own title
# and h4+ is detail; a table of contents that lists everything is a wall, and
# the README alone has well over a hundred headings.
_TOC_LEVELS = {"h2", "h3"}

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_ABSOLUTE = re.compile(r"^[a-z][a-z0-9+.-]*:|^//", re.IGNORECASE)

# A line that is nothing but HTML tags: `<div align="center">`, `</div>`,
# `<img ...>`, `<br />`. See `_strip_html_only_lines`.
_HTML_ONLY_LINE = re.compile(r"^\s*(?:</?[a-zA-Z][^<>]*>\s*)+$")
_FENCE = re.compile(r"^\s*(?:```|~~~)")

# `<summary>Windows (PowerShell)</summary>` -- the visible label of a
# `<details>` block. See `_strip_html_only_lines`.
_SUMMARY_LINE = re.compile(
    r"^\s*<summary[^<>]*>(.*)</summary>\s*$", re.IGNORECASE | re.DOTALL
)
_ANY_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")
# A CommonMark autolink. It opens with a letter, so the HTML-only rule would
# otherwise treat the whole line as a tag and delete the URL.
_AUTOLINK_LINE = re.compile(r"^\s*<[a-zA-Z][a-zA-Z0-9+.-]*://[^<>\s]+>\s*$")
# A whole line that both opens with a tag and closes with a closing tag, with
# prose in between. Requiring a real `</tag>` at the end keeps autolinks such
# as `<https://example.com>` out, and requiring the line to *start* with a tag
# keeps inline HTML inside a sentence out.
_WRAPPED_TEXT_LINE = re.compile(r"^\s*<[a-zA-Z][^<>]*>.*</[a-zA-Z][^<>]*>\s*$")


class Heading(NamedTuple):
    """One entry in a document's table of contents."""

    anchor: str
    text: str
    level: int


class RenderedDocument(NamedTuple):
    slug: str
    title: str
    summary: str
    html: str
    headings: tuple[Heading, ...]
    github_url: str


def _strip_html_only_lines(source: str) -> str:
    """Drop lines that are nothing but HTML tags, outside fenced code.

    With `html: False` the parser escapes raw HTML and shows it as literal
    text, which is the safe default and the wrong *display*: the README opens
    with a centred `<div>`/`<img>` banner, and rendering it verbatim puts five
    lines of `&lt;div align=&quot;center&quot;&gt;` above the title.

    So HTML-only lines are removed before parsing rather than un-escaped
    after it -- nothing is ever promoted from text to markup, which is the
    property that makes the escaping worth having. Inline HTML in the middle
    of a sentence is left alone and still renders escaped: it carries words,
    and silently deleting those would lose content.

    A `<summary>` line is unwrapped to its text rather than dropped: it is
    the only title the eleven `<details>` sections in the README have, and
    those sections are now always visible because the `<details>` wrapper
    went with the HTML-only lines.

    Fenced code blocks are tracked and never touched. This project's
    documentation is full of XML-ish and HTML-ish snippets inside fences, and
    a pass that ignored fences would quietly eat lines out of the examples.
    """

    kept: list[str] = []
    in_fence = False
    for line in source.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            kept.append(line)
            continue
        if not in_fence:
            if _AUTOLINK_LINE.match(line):
                # `<https://example.com>` is a CommonMark autolink, not a tag,
                # but it opens with a letter so the HTML-only rule below eats
                # the whole line and the URL disappears with no trace. No
                # curated document has one today; a future one would lose
                # content silently, which is the failure worth pre-empting.
                kept.append(line)
                continue
            if _HTML_ONLY_LINE.match(line):
                continue
            wrapped = _WRAPPED_TEXT_LINE.match(line)
            if wrapped is not None and _SUMMARY_LINE.match(line) is None:
                # A whole line that is one HTML element wrapped around plain
                # prose -- the README's image captions are
                # `<p><em>Claude Code running through the proxy.</em></p>`.
                # These are not HTML-only, so the rule above leaves them, and
                # escaping then shows the reader raw markup instead of a
                # caption. Unwrap to the text: emphasis is lost, literal tags
                # in the middle of the page are not. Only a line that both
                # opens with a tag and closes with one qualifies, so inline
                # HTML inside a sentence is still left alone as documented.
                text = _ANY_TAG.sub("", line).strip()
                if text:
                    kept.append(text)
                    continue
            summary = _SUMMARY_LINE.match(line)
            if summary is not None:
                # The `<details>` wrapper is gone with the HTML-only lines
                # above, so its label has to survive as something -- it is the
                # only title those eleven README sections have. Bold lead-in,
                # which is also what the house style wants: the content is now
                # always visible instead of hidden behind a disclosure.
                label = _ANY_TAG.sub("", summary.group(1)).strip()
                kept.append(f"**{label}**" if label else "")
                continue
        kept.append(line)
    return "\n".join(kept)


def _slugify(text: str) -> str:
    return _NON_SLUG.sub("-", text.strip().lower()).strip("-") or "section"


@lru_cache(maxsize=1)
def _parser() -> MarkdownIt:
    # CommonMark with `html` off, plus the two block constructs the project's
    # documentation actually uses beyond it. `linkify` is deliberately not
    # enabled: it needs an optional dependency that is not declared, and a
    # preset that quietly turns into a hard import error at startup is a worse
    # bug than a bare URL rendering as text.
    parser = MarkdownIt("commonmark", {"html": False, "breaks": False})
    parser.enable("table")
    parser.enable("strikethrough")
    return parser


def _anchor_for(text: str, used: dict[str, int]) -> str:
    base = _slugify(text)
    seen = used.get(base, 0)
    used[base] = seen + 1
    return base if seen == 0 else f"{base}-{seen}"


def _decorate(tokens: list[Token]) -> tuple[Heading, ...]:
    """Add ids, classes and link targets in place; collect the headings.

    Done on the token stream rather than on the rendered HTML string: a
    regex pass over output would happily rewrite an `href=` that appears
    inside a fenced code block, which is exactly the sort of thing this
    project's documentation is full of.
    """

    headings: list[Heading] = []
    used: dict[str, int] = {}

    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            text = inline.content if inline is not None else ""
            anchor = _anchor_for(text, used)
            token.attrSet("id", anchor)
            if token.tag in _TOC_LEVELS:
                headings.append(Heading(anchor, text, int(token.tag[1])))

        elif token.type == "table_open":
            # Reuse the guide's table styling rather than inventing a second
            # table skin for the same dashboard.
            token.attrJoin("class", "guide-table")

        elif token.type == "link_open":
            href = token.attrGet("href") or ""
            if isinstance(href, str) and href and not href.startswith("#"):
                if not _ABSOLUTE.match(href):
                    href = resolve_relative_link(href)
                    token.attrSet("href", href)
                if not href.startswith("#"):
                    # An admin page that navigates itself away on a doc link
                    # loses whatever unsaved settings were on another tab.
                    token.attrSet("target", "_blank")
                    token.attrSet("rel", "noopener noreferrer")

        elif token.type == "image":
            src = token.attrGet("src") or ""
            if isinstance(src, str) and src and not _ABSOLUTE.match(src):
                token.attrSet("src", resolve_relative_link(src))
            token.attrSet("loading", "lazy")

        elif token.type == "inline" and token.children:
            _decorate(token.children)

    return tuple(headings)


def _render_markdown_with_headings(source: str) -> tuple[str, tuple[Heading, ...]]:
    """Markdown in, page HTML and table of contents out.

    Kept separate from `render_document` so the rendering rules can be
    exercised on a three-line input rather than only through a 1,199-line
    file whose prose changes independently of this code.
    """

    parser = _parser()
    tokens = parser.parse(_strip_html_only_lines(source))
    headings = _decorate(tokens)
    return parser.renderer.render(tokens, parser.options, {}), headings


def _render_markdown(source: str) -> str:
    return _render_markdown_with_headings(source)[0]


@lru_cache(maxsize=len(DOCUMENT_BY_SLUG))
def render_document(slug: str) -> RenderedDocument | None:
    """Render one curated document, or ``None`` if the slug is not one.

    Cached: the documents cannot change while the process runs -- they are
    files inside the installed package -- so a re-render per page view would
    be pure waste on a 1,199-line input.
    """

    source = load_markdown(slug)
    if source is None:
        return None
    document: Document = DOCUMENT_BY_SLUG[slug]

    html, headings = _render_markdown_with_headings(source)

    return RenderedDocument(
        slug=document.slug,
        title=document.title,
        summary=document.summary,
        html=html,
        headings=headings,
        github_url=document.github_url,
    )
