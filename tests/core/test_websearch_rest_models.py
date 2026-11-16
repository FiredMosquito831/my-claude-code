"""Unit tests for neutral core websearch models and pure REST schemas."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from my_claude_code.core.websearch import (
    WebFetchRestRequest,
    WebFetchRestResponse,
    WebSearchRequestOptions,
    WebSearchResponse,
    WebSearchRestRequest,
    WebSearchRestResponse,
    WebSearchRestResult,
    WebSearchRestUsage,
    WebSearchResultItem,
)


def test_core_result_and_response_keep_positional_compatibility() -> None:
    item = WebSearchResultItem("Title", "https://example.org", "Snippet", None, None)
    assert item.score is None
    assert item.author is None
    assert item.favicon is None
    assert item.site_name is None
    assert item.highlights is None
    assert item.highlight_scores is None

    response = WebSearchResponse("exa", "query", (item,), 0, 0.005)
    assert response.answer is None
    assert response.request_id is None
    assert response.provider_response_time_ms is None
    assert response.usage_credits is None
    assert response.images is None


def test_request_options_is_frozen_and_filters_none_fields() -> None:
    options = WebSearchRequestOptions(
        time_range="week", search_depth="deep", country="de"
    )
    attr = "country"
    with pytest.raises(FrozenInstanceError):
        setattr(options, attr, "fr")
    assert options.non_empty_dict() == {
        "time_range": "week",
        "search_depth": "deep",
        "country": "de",
    }


def test_websearch_rest_request_defaults_aliases_and_to_request_options() -> None:
    minimal = WebSearchRestRequest(query="  python 3.14  ")
    assert minimal.query == "python 3.14"
    assert minimal.max_results is None
    assert minimal.format == "mcc"
    assert minimal.include_digest is False
    assert minimal.fetch_missing_content is False
    assert minimal.fetch_top_n == 3
    assert minimal.to_request_options() is None

    max_only = WebSearchRestRequest(query="q", max_results=5)
    assert max_only.to_request_options() is None

    aliased = WebSearchRestRequest.model_validate(
        {
            "query": "test",
            "provider": " Exa ",
            "include_domains": [" docs.python.org ", ""],
            "exclude_domains": ["spam.invalid"],
            "time_range": "month",
            "search_depth": "deep",
            "topic": "news",
            "country": "us",
            "language": "en",
            "include_content": True,
            "include_answer": True,
            "raw_content_format": "markdown",
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "format": "digest",
            "include_digest": True,
        }
    )
    assert aliased.provider == "exa"
    assert aliased.allowed_domains == ["docs.python.org"]
    assert aliased.blocked_domains == ["spam.invalid"]
    opts = aliased.to_request_options()
    assert opts == WebSearchRequestOptions(
        time_range="month",
        start_date="2026-01-01",
        end_date="2026-02-01",
        search_depth="deep",
        topic="news",
        country="us",
        language="en",
        include_content=True,
        include_answer=True,
        raw_content_format="markdown",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 2001},
        {"query": "ok", "max_results": 0},
        {"query": "ok", "max_results": 51},
        {"query": "ok", "max_results": True},
        {"query": "ok", "fetch_top_n": False},
        {"query": "ok", "time_range": "decade"},
        {"query": "ok", "search_depth": "ultra"},
        {"query": "ok", "start_date": "not-a-date"},
        {"query": "ok", "start_date": "2026-03-01", "end_date": "2026-02-01"},
        {"query": "ok", "synthesize": True},
        {"query": "ok", "format": "tavily"},
        {"query": "ok", "unknown_field": 1},
    ],
)
def test_websearch_rest_request_rejects_invalid_inputs(payload: dict) -> None:
    with pytest.raises(ValidationError):
        WebSearchRestRequest.model_validate(payload)


def test_webfetch_rest_request_validation_and_response_shapes() -> None:
    req = WebFetchRestRequest(url="  https://example.org/page  ")
    assert req.url == "https://example.org/page"
    assert req.extractor is None
    assert req.max_chars is None
    assert req.allow_private_networks is None

    for bad in (
        {"url": "   "},
        {"url": "https://example.org", "extractor": "readability"},
        {"url": "https://example.org", "max_chars": 199},
        {"url": "https://example.org", "max_chars": 200_001},
        {"url": "https://example.org", "max_chars": True},
        {"url": "https://example.org", "extra": "nope"},
    ):
        with pytest.raises(ValidationError):
            WebFetchRestRequest.model_validate(bad)

    search_resp = WebSearchRestResponse(
        id="ws_1",
        query="q",
        provider="exa",
        fallback_used=False,
        providers_tried=["exa"],
        supports_domain_filters=True,
        results=[WebSearchRestResult(title="T", url="https://a.io", snippet="S")],
        usage=WebSearchRestUsage(),
    )
    assert search_resp.object == "web_search"
    assert search_resp.synthesis is None
    assert search_resp.digest is None

    fetch_resp = WebFetchRestResponse(
        id="wf_1",
        url="https://a.io",
        title="Title",
        media_type="text/plain",
        extractor_used="trafilatura",
        truncated=False,
        content="Body",
    )
    assert fetch_resp.object == "web_fetch"
