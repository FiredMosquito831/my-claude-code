"""The text ``web_fetch`` returns for an HTML page (7.120.0).

Two extractors, chosen per fetch by ``WEB_FETCH_EXTRACTOR``:

* ``trafilatura`` -- the new default (RESEARCH-DDG-WEBSEARCH.md R3; USER
  DECISIONS 2026-10-25 Q6/Q7). The page's main content as markdown, links and
  tables kept, comments dropped. It is a heavy import -- 648 ms and 360
  modules measured on a cold interpreter (2026-10-30), the reason it is
  imported inside :func:`markdown` and never at module level:
  ``tests/contracts/test_startup_import_cost.py`` refuses a server start that
  loads it.
* ``basic`` -- every text node, one per line, in document order: what every
  release before 7.120.0 returned, byte for byte (``parsers.HTMLTextParser``).
  The escape hatch exists because the shape a reader sees changes.

A page never gets worse. When trafilatura answers nothing, or fewer than
:data:`_WEB_FETCH_EXTRACTOR_MIN_CHARS` characters, the basic parser's text
answers instead. Only trafilatura's text is cut at the 24,000-character cap
with a one-line marker naming the extracted total; the basic path keeps the
silent cut it always had, which is what makes ``basic``'s bytes the ones the
releases before this one returned (R3's cap and marker, Q8).
"""

from my_claude_code.config.constants import WEB_FETCH_EXTRACTOR_DEFAULT

from .constants import _MAX_FETCH_CHARS, _WEB_FETCH_EXTRACTOR_MIN_CHARS

#: The extractor whose text is markdown (the default; ``config.constants``
#: names both values and Settings validates against them).
EXTRACTOR_TRAFILATURA = WEB_FETCH_EXTRACTOR_DEFAULT


def markdown(html: str) -> str | None:
    """trafilatura's markdown for ``html``, or ``None`` when it finds nothing.

    The import is inside this function on purpose, and this function is
    reached only from the fetch path (``outbound._run_web_fetch``): a server
    start, a search or any other caller must not pay 648 ms for a package it
    never asks a question of.
    """

    import trafilatura

    return trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        include_comments=False,
    )


def text_for(html: str, basic_text: str, *, extractor: str) -> str:
    """The ``data`` string ``web_fetch`` returns for one HTML page.

    ``basic_text`` is today's parser's answer for the same page. It is used
    when the operator chose ``basic`` and whenever the extractor cannot answer
    for the page, so the fetch never returns less than it did before 7.120.0.
    """

    if extractor != EXTRACTOR_TRAFILATURA:
        return basic_text[:_MAX_FETCH_CHARS]
    extracted = markdown(html)
    if extracted is None or len(extracted.strip()) < _WEB_FETCH_EXTRACTOR_MIN_CHARS:
        return basic_text[:_MAX_FETCH_CHARS]
    return capped(extracted)


def capped(text: str) -> str:
    """``text`` cut at the cap, with a one-line marker naming the total."""

    if len(text) <= _MAX_FETCH_CHARS:
        return text
    marker = truncation_marker(len(text))
    return text[: _MAX_FETCH_CHARS - len(marker)] + marker


def truncation_marker(total_chars: int) -> str:
    """The line a cut page ends with: the cap, then what was extracted."""

    return (
        f"\n\n[web_fetch: truncated to {_MAX_FETCH_CHARS} of "
        f"{total_chars} characters extracted]"
    )
