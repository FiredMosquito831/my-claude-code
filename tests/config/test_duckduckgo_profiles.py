"""The DuckDuckGo page profiles are data, and every row of it is readable (7.119.0).

``config/duckduckgo_profiles.py`` is the whole description of what the keyless
DuckDuckGo hop asks and how it reads the answer; a markup change is meant to
be an edit to one row there. These checks make a bad edit fail CI instead of
silently matching nothing: every selector parses in the small declared form,
every option a request forwards is one the ``ddgs`` provider really has, and
the selector form refuses what it does not support.

7.119.1 (RESEARCH-DDG-WEBSEARCH.md 5 R2, USER DECISIONS Q12): the requests are
the html form first and the lite page second, the html form with R2's fields
and headers, both with one static browser-class User-Agent -- declared here
and pinned here, so an edit that drops one of them fails CI.
"""

import pytest

from my_claude_code.api.web_tools import duckduckgo_lite
from my_claude_code.config import duckduckgo_profiles as profiles
from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG


def test_every_declared_selector_parses() -> None:
    for profile in profiles.PAGE_PROFILES:
        compiled = duckduckgo_lite.compile_profile(profile)
        assert compiled.kind == profile.kind
        assert compiled.link.tag == "a", profile.kind
        assert len(compiled.rows) == len(profile.rows), profile.kind


def test_the_page_kinds_are_the_two_no_javascript_pages() -> None:
    kinds = [profile.kind for profile in profiles.PAGE_PROFILES]
    assert kinds == ["html", "lite"]
    assert len(set(kinds)) == len(kinds)
    assert all(profile.source for profile in profiles.PAGE_PROFILES)
    # Every request is read by a declared page kind, in the same order.
    assert [request.kind for request in profiles.REQUESTS] == kinds


def test_the_html_row_chain_is_r2s() -> None:
    """``div.result:not(.result--ad)`` / ``div.web-result``, ``a.result__a``,
    ``.result__snippet``; lite: ``a.result-link``, ``td.result-snippet``."""

    html, lite = profiles.PAGE_PROFILES
    assert html.rows == ("div.result", "div.web-result")
    assert html.skip == ("div.result--ad",)
    assert (html.link, html.snippet) == ("a.result__a", ".result__snippet")
    assert lite.rows == ()
    assert (lite.link, lite.snippet) == ("a.result-link", "td.result-snippet")


def test_the_requests_forward_only_options_the_ddgs_provider_has() -> None:
    declared = {option.env for option in WEBSEARCH_CATALOG["ddgs"].advanced_options}
    for request in profiles.REQUESTS:
        assert dict(request.option_fields) == {
            "DDGS_REGION": "kl",
            "DDGS_TIMELIMIT": "df",
        }, request.kind
        assert {option for option, _ in request.option_fields} <= declared
        assert request.endpoint.startswith("https://"), request.kind
        assert request.method in {"GET", "POST"}, request.kind
        assert request.source, request.kind


def test_the_html_form_is_asked_first_as_r2_declares_it() -> None:
    """``POST https://html.duckduckgo.com/html/`` with ``q`` and ``b``; headers
    ``Referer`` and the ``Sec-Fetch-*`` set; then ``GET`` of the lite page."""

    assert profiles.REQUESTS == (profiles.HTML_REQUEST, profiles.LITE_REQUEST)
    html = profiles.HTML_REQUEST
    assert (html.kind, html.method, html.endpoint) == (
        "html",
        "POST",
        "https://html.duckduckgo.com/html/",
    )
    assert (html.query_field, html.fixed_fields) == ("q", (("b", ""),))
    assert html.headers == (
        ("User-Agent", profiles.BROWSER_USER_AGENT),
        ("Referer", "https://html.duckduckgo.com/"),
        ("Sec-Fetch-Dest", "document"),
        ("Sec-Fetch-Mode", "navigate"),
        ("Sec-Fetch-Site", "same-origin"),
        ("Sec-Fetch-User", "?1"),
    )
    lite = profiles.LITE_REQUEST
    assert (lite.kind, lite.method, lite.endpoint) == (
        "lite",
        "GET",
        "https://lite.duckduckgo.com/lite/",
    )
    assert (lite.query_field, lite.fixed_fields) == ("q", ())
    assert lite.headers == (("User-Agent", profiles.BROWSER_USER_AGENT),)


def test_both_requests_send_one_static_browser_class_user_agent() -> None:
    """Q12: a browser-class UA, as declared data, for the DuckDuckGo hop only."""

    agent = profiles.BROWSER_USER_AGENT
    assert agent.startswith("Mozilla/5.0 (")
    assert "Gecko/" in agent and "Firefox/" in agent
    for product in ("my-claude-code", "free-claude-code", "python-httpx", "{"):
        assert product not in agent
    for request in profiles.REQUESTS:
        assert dict(request.headers)["User-Agent"] == agent, request.kind


def test_the_bot_check_is_declared() -> None:
    assert frozenset({202}) == profiles.CHALLENGE_STATUSES
    assert "anomaly-modal" in profiles.CHALLENGE_MARKERS
    assert all(marker.strip() for marker in profiles.CHALLENGE_MARKERS)


@pytest.mark.parametrize(
    ("text", "tag", "classes", "attributes"),
    [
        ("a.result-link", "a", {"result-link"}, ()),
        (".result__snippet", None, {"result__snippet"}, ()),
        ("div.result.result--ad", "div", {"result", "result--ad"}, ()),
        ('input[name="s"]', "input", set(), (("name", "s"),)),
        ('form[action="/lite/"]', "form", set(), (("action", "/lite/"),)),
        ("form[id]", "form", set(), (("id", None),)),
    ],
)
def test_the_small_selector_form(
    text: str, tag: str | None, classes: set[str], attributes: tuple
) -> None:
    selector = duckduckgo_lite.parse_selector(text)
    assert selector.tag == tag
    assert selector.classes == frozenset(classes)
    assert selector.attributes == attributes


@pytest.mark.parametrize(
    "text",
    ["", "   ", "div > a", "div a", "a.result-link:hover", "a[href=unquoted]", "A"],
)
def test_a_selector_outside_the_form_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="not a page-profile selector"):
        duckduckgo_lite.parse_selector(text)
