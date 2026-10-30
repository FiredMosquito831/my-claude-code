"""The DuckDuckGo page profiles are data, and every row of it is readable (7.119.0).

``config/duckduckgo_profiles.py`` is the whole description of what the keyless
DuckDuckGo hop asks and how it reads the answer; a markup change is meant to
be an edit to one row there. These checks make a bad edit fail CI instead of
silently matching nothing: every selector parses in the small declared form,
every option the request forwards is one the ``ddgs`` provider really has,
and the selector form refuses what it does not support.
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


def test_the_page_kinds_are_the_two_no_javascript_pages() -> None:
    kinds = [profile.kind for profile in profiles.PAGE_PROFILES]
    assert kinds == ["lite", "html"]
    assert len(set(kinds)) == len(kinds)
    assert all(profile.source for profile in profiles.PAGE_PROFILES)


def test_the_request_forwards_only_options_the_ddgs_provider_has() -> None:
    declared = {option.env for option in WEBSEARCH_CATALOG["ddgs"].advanced_options}
    assert dict(profiles.LITE_REQUEST.option_fields) == {
        "DDGS_REGION": "kl",
        "DDGS_TIMELIMIT": "df",
    }
    assert {option for option, _ in profiles.LITE_REQUEST.option_fields} <= declared
    assert profiles.LITE_REQUEST.endpoint.startswith("https://")


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
