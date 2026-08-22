"""The dashboard's Docs page: routing, safety, and rendering mechanics.

Deliberately asserts nothing about the *prose* of the bundled documents.
They are edited constantly and independently of this feature; a test that
pinned a sentence would be a tripwire on someone else's work rather than a
check on this one. What is asserted is mechanism: that a slug outside the
curated table cannot reach the filesystem, that raw HTML never becomes
markup, and that the structures the page depends on (heading ids, table
classes, rewritten links) are actually produced.
"""

import re

from fastapi.testclient import TestClient

from my_claude_code.api import docs_content, docs_render
from my_claude_code.api.docs_content import DOCUMENTS, GITHUB_BLOB_BASE
from tests.api.support import create_test_app


def _local_client() -> TestClient:
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


# --------------------------------------------------------------------- routes


def test_the_index_lists_every_curated_document() -> None:
    with _local_client() as client:
        response = client.get("/admin/api/docs")

    assert response.status_code == 200
    slugs = [entry["slug"] for entry in response.json()["documents"]]
    assert slugs == [document.slug for document in DOCUMENTS]


def test_every_listed_document_carries_a_github_link() -> None:
    with _local_client() as client:
        documents = client.get("/admin/api/docs").json()["documents"]

    for entry in documents:
        assert entry["github_url"].startswith(GITHUB_BLOB_BASE)
        assert entry["title"]
        assert entry["summary"]


def test_each_curated_document_renders() -> None:
    with _local_client() as client:
        for document in DOCUMENTS:
            response = client.get(f"/admin/api/docs/{document.slug}")
            assert response.status_code == 200, document.slug
            payload = response.json()
            assert payload["slug"] == document.slug
            assert payload["html"], f"{document.slug} rendered to nothing"
            assert payload["github_url"] == document.github_url


def test_an_unknown_document_is_a_404_not_a_500() -> None:
    """The page asks for whatever slug it was given; a typo must not be a
    server error, and must not surface a stack trace to the browser."""

    with _local_client() as client:
        response = client.get("/admin/api/docs/does-not-exist")

    assert response.status_code == 404


def test_a_path_traversal_attempt_cannot_reach_the_filesystem() -> None:
    """Slugs are matched against the curated table, never joined onto a
    directory, so traversal is impossible by construction. This asserts the
    property rather than the sanitiser: every one of these must 404, and
    none may return the contents of anything.
    """

    attempts = (
        "../../../../etc/passwd",
        "..%2F..%2F..%2Fetc%2Fpasswd",
        "....//....//pyproject.toml",
        "%2e%2e%2fpyproject.toml",
        "readme/../../../pyproject.toml",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
    )
    with _local_client() as client:
        for attempt in attempts:
            response = client.get(f"/admin/api/docs/{attempt}")
            assert response.status_code in {404, 400, 405}, attempt
            if response.status_code == 404 and response.headers.get(
                "content-type", ""
            ).startswith("application/json"):
                assert "html" not in response.json(), attempt


def test_a_slug_that_is_not_curated_never_reaches_the_loader() -> None:
    """The unit-level mirror of the route test above."""

    for attempt in ("../README", "README.md", "pyproject.toml", "", "."):
        assert docs_content.load_markdown(attempt) is None, attempt
        assert docs_render.render_document(attempt) is None, attempt


def test_the_docs_endpoints_are_local_only() -> None:
    remote = TestClient(create_test_app(), client=("203.0.113.10", 50000))
    with remote as client:
        assert client.get("/admin/api/docs").status_code == 403
        assert client.get("/admin/api/docs/readme").status_code == 403


# ------------------------------------------------------------------ rendering


def test_raw_html_in_a_document_is_escaped_never_injected() -> None:
    """A renderer that emits raw HTML is a footgun the moment anything other
    than a trusted repository file is fed to it, and the page it would inject
    into is already authenticated to a local admin API.
    """

    html = docs_render._render_markdown(
        "Hello <script>alert(1)</script> and <b onclick='x'>bold</b>.\n"
    )

    # The property that matters is that no element was *created*, not that the
    # word "onclick" is absent from the page: it survives as literal text
    # inside `&lt;b onclick='x'&gt;`, which is inert precisely because the
    # angle brackets are escaped. Asserting on the substring would fail here
    # while proving nothing, so assert on the tags instead -- markdown itself
    # is only allowed to produce a paragraph for this input.
    tags = set(re.findall(r"<\s*(/?[a-zA-Z][a-zA-Z0-9]*)", html))
    assert tags <= {"p", "/p"}, tags

    assert "&lt;script&gt;" in html
    assert "&lt;b onclick=" in html


def test_html_only_lines_are_dropped_rather_than_shown_as_text() -> None:
    source = '<div align="center">\n\n# Title\n\n</div>\n'
    assert "&lt;div" not in docs_render._render_markdown(source)


def test_html_inside_a_fenced_block_survives_untouched() -> None:
    """The documents are full of markup samples inside fences. A pass that
    ignored fences would silently eat lines out of the examples."""

    source = '```html\n<div align="center">\n</div>\n```\n'
    stripped = docs_render._strip_html_only_lines(source)

    assert '<div align="center">' in stripped
    assert "</div>" in stripped


def test_a_details_summary_keeps_its_label_as_a_heading() -> None:
    """`<details>` wrappers are dropped -- the house style has no accordions
    -- but the summary is the only title those sections have."""

    source = "<details>\n<summary><b>Windows (PowerShell)</b></summary>\n\nBody.\n\n</details>\n"
    html = docs_render._render_markdown(source)

    assert "Windows (PowerShell)" in html
    assert "&lt;summary&gt;" not in html


def test_headings_get_unique_anchor_ids() -> None:
    source = "## Install\n\ntext\n\n## Install\n\ntext\n"
    html = docs_render._render_markdown(source)

    assert 'id="install"' in html
    assert 'id="install-1"' in html


def test_the_table_of_contents_lists_the_documents_own_headings() -> None:
    rendered = docs_render.render_document("readme")

    assert rendered is not None
    assert rendered.headings, "no table of contents for a 1,000-line document"
    for heading in rendered.headings:
        assert heading.level in {2, 3}
        assert f'id="{heading.anchor}"' in rendered.html


def test_tables_reuse_the_dashboards_existing_table_style() -> None:
    source = "| a | b |\n| - | - |\n| 1 | 2 |\n"
    html = docs_render._render_markdown(source)

    assert "<table" in html
    assert 'class="guide-table"' in html


def test_the_block_constructs_the_documents_actually_use_all_render() -> None:
    source = (
        "# H1\n\n## H2\n\nA paragraph with `inline code`.\n\n"
        "- one\n- two\n\n1. first\n\n> quoted\n\n"
        "```python\nx = 1\n```\n\n"
        "![alt](https://example.invalid/i.png)\n"
    )
    html = docs_render._render_markdown(source)

    for fragment in (
        "<h1",
        "<h2",
        "<code>",
        "<ul>",
        "<ol>",
        "<blockquote>",
        "<pre>",
        "<img",
    ):
        assert fragment in html, fragment


def test_a_link_to_another_bundled_document_stays_in_the_dashboard() -> None:
    """A cross-reference must not eject the reader into a browser tab for a
    document the page is already showing."""

    html = docs_render._render_markdown("See [usage](docs/USAGE.md).\n")

    assert 'href="#doc-usage"' in html
    assert "target=" not in html


def test_a_link_to_a_source_file_goes_to_github() -> None:
    """The dashboard has nothing to show for `src/...`, so the only useful
    destination is the repository."""

    html = docs_render._render_markdown("See [settings](src/my_claude_code/x.py).\n")

    assert f'href="{GITHUB_BLOB_BASE}/src/my_claude_code/x.py"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_an_absolute_link_is_left_alone() -> None:
    html = docs_render._render_markdown("[x](https://example.invalid/a)\n")

    assert 'href="https://example.invalid/a"' in html


def test_a_relative_image_is_pointed_at_the_repository() -> None:
    html = docs_render._render_markdown("![shot](assets/pic.png)\n")

    assert f'src="{GITHUB_BLOB_BASE}/assets/pic.png"' in html
    assert 'loading="lazy"' in html


class TestWrappedProseIsNotShownAsMarkup:
    """The bug a browser found and the unit tests did not.

    ``_strip_html_only_lines`` drops lines that are *only* tags. The README's
    image captions are ``<p><em>text</em></p>`` -- tags wrapped around prose,
    so they survived and were escaped, putting literal markup on the page.
    """

    def test_a_caption_line_renders_as_prose_not_as_tags(self):
        html = docs_render._render_markdown(
            "# Title\n\n<p><em>Claude Code running through the proxy.</em></p>\n"
        )
        assert "Claude Code running through the proxy." in html
        assert "&lt;p&gt;" not in html
        assert "&lt;em&gt;" not in html

    def test_inline_html_inside_a_sentence_is_still_left_alone(self):
        # Documented behaviour: it carries words, and deleting them loses
        # content. It must still be escaped rather than unwrapped.
        html = docs_render._render_markdown("Set the <b>MODEL</b> value first.\n")
        assert "&lt;b&gt;" in html

    def test_an_autolink_is_not_mistaken_for_a_wrapped_line(self):
        html = docs_render._render_markdown("<https://example.com>\n")
        assert "example.com" in html

    def test_html_wrapped_prose_inside_a_fence_survives_untouched(self):
        html = docs_render._render_markdown(
            "```html\n<p><em>kept verbatim</em></p>\n```\n"
        )
        assert "&lt;p&gt;&lt;em&gt;kept verbatim" in html
