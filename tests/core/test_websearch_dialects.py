"""Unit tests for pure REST websearch dialect schemas and translators (IV.16 & IX.9)."""

import pytest
from pydantic import ValidationError

from my_claude_code.core.websearch import (
    BatchFetchOutcome,
    BraveSearchQuery,
    DeepSeekFetchRequest,
    DeepSeekSearchRequest,
    ExaContentsRequest,
    ExaSearchRequest,
    SearxngSearchQuery,
    TavilyExtractRequest,
    TavilySearchRequest,
    WebFetchRestResponse,
    WebSearchRestResponse,
    WebSearchRestResult,
    WebSearchRestSynthesis,
    WebSearchRestUsage,
    WebSearchSynthesisCitation,
    to_brave_search_payload,
    to_deepseek_fetch_payload,
    to_deepseek_search_payload,
    to_exa_contents_payload,
    to_exa_search_payload,
    to_searxng_search_payload,
    to_tavily_extract_payload,
    to_tavily_search_payload,
    translate_websearch_response,
)


def _sample_rest_response(
    *,
    with_metadata: bool = True,
    with_synthesis: bool = False,
) -> WebSearchRestResponse:
    if with_metadata:
        hit = WebSearchRestResult(
            title="Doc Title",
            url="https://docs.example.org/page",
            snippet="Short snippet",
            content="Full extracted markdown",
            published="2026-10-01",
            score=0.92,
            author="Ada",
            favicon="https://docs.example.org/favicon.ico",
            site_name="Example Docs",
            highlights=["Highlighted passage"],
            highlight_scores=[0.88],
        )
        cost = 0.005
        credits: float | None = 2.0
        rt_ms: float | None = 420.0
        req_id: str | None = "upstream-123"
        images: list[str | dict[str, str]] | None = ["https://docs.example.org/img.png"]
        answer: str | None = "Upstream answer"
    else:
        hit = WebSearchRestResult(
            title="Plain Title",
            url="https://plain.example.org/",
            snippet="Plain snippet",
        )
        cost = None
        credits = None
        rt_ms = None
        req_id = None
        images = None
        answer = None

    synthesis = (
        WebSearchRestSynthesis(
            model="test/synth-model",
            answer="Grounded answer [1].",
            citations=[
                WebSearchSynthesisCitation(
                    index=1,
                    url=hit.url,
                    title=hit.title,
                )
            ],
        )
        if with_synthesis
        else None
    )

    return WebSearchRestResponse(
        id="ws_abc123",
        query="  python 3.14  ".strip(),
        provider="exa",
        fallback_used=False,
        providers_tried=["exa"],
        supports_domain_filters=True,
        answer=answer,
        synthesis=synthesis,
        results=[hit],
        digest=None,
        provider_response_time_ms=rt_ms,
        request_id=req_id,
        usage=WebSearchRestUsage(
            search_requests=1,
            search_cost_usd=cost,
            usage_credits=credits,
        ),
        images=images,
    )


def test_tavily_search_request_translation_and_validation() -> None:
    req = TavilySearchRequest.model_validate(
        {
            "query": "  tavily sdk  ",
            "search_depth": "ultra-fast",
            "topic": "news",
            "max_results": 7,
            "days": 5,
            "include_answer": "advanced",
            "include_raw_content": "markdown",
            "include_images": True,
            "include_domains": [" docs.python.org ", ""],
            "exclude_domains": ["spam.invalid"],
            "chunks_per_source": 3,
            "auto_parameters": True,
            "include_usage": True,
            "include_image_descriptions": True,
            "api_key": " tvly-key ",
        }
    )
    rest = req.to_rest_search_request()
    assert rest.query == "tavily sdk"
    assert rest.search_depth == "fast"
    assert rest.time_range == "week"
    assert rest.max_results == 7
    assert rest.include_answer is True
    assert rest.include_content is True
    assert rest.raw_content_format == "markdown"
    assert rest.allowed_domains == ["docs.python.org"]
    assert rest.blocked_domains == ["spam.invalid"]
    assert rest.format == "tavily"

    # Explicit time_range takes precedence over days; advanced -> deep
    req2 = TavilySearchRequest(
        query="q",
        search_depth="advanced",
        time_range="year",
        days=1,
        include_raw_content=True,
    )
    rest2 = req2.to_rest_search_request()
    assert rest2.search_depth == "deep"
    assert rest2.time_range == "year"
    assert rest2.include_content is True
    assert rest2.raw_content_format is None

    for bad in (
        {"query": "  "},
        {"query": "ok", "max_results": True},
        {"query": "ok", "days": False},
        {"query": "ok", "time_range": "decade"},
        {"query": "ok", "start_date": "2026-04-01", "end_date": "2026-03-01"},
    ):
        with pytest.raises(ValidationError):
            TavilySearchRequest.model_validate(bad)


def test_tavily_extract_and_response_payload() -> None:
    single = TavilyExtractRequest.model_validate(
        {"urls": " https://a.io ", "format": "text"}
    )
    assert single.normalized_urls() == ["https://a.io"]
    assert single.effective_extractor() == "basic"

    for bad in (
        {"urls": []},
        {"urls": [""]},
        {"urls": [f"https://example.org/{i}" for i in range(21)]},
    ):
        with pytest.raises(ValidationError):
            TavilyExtractRequest.model_validate(bad)

    payload = to_tavily_search_payload(
        _sample_rest_response(with_metadata=True, with_synthesis=True),
        include_images=True,
    )
    assert payload["answer"] == "Grounded answer [1]."
    assert payload["images"] == ["https://docs.example.org/img.png"]
    assert payload["response_time"] == 0.42
    assert payload["usage"] == {"credits": 2.0}
    assert payload["request_id"] == "upstream-123"
    assert payload["results"][0]["published_date"] == "2026-10-01"
    assert payload["results"][0]["score"] == 0.92
    assert payload["results"][0]["raw_content"] == "Full extracted markdown"

    extract_payload = to_tavily_extract_payload(
        [
            BatchFetchOutcome(url="https://good.io", title="Good", content="Body"),
            BatchFetchOutcome(url="https://bad.io", error="HTTP 500"),
        ],
        request_id="req_1",
        elapsed_ms=250.0,
    )
    assert extract_payload == {
        "results": [{"url": "https://good.io", "raw_content": "Body", "title": "Good"}],
        "failed_results": [{"url": "https://bad.io", "error": "HTTP 500"}],
        "response_time": 0.25,
        "request_id": "req_1",
    }


def test_exa_request_response_and_contents_translation() -> None:
    req = ExaSearchRequest.model_validate(
        {
            "query": " exa search ",
            "type": "lite",
            "numResults": 12,
            "includeDomains": ["arxiv.org"],
            "excludeDomains": ["pinterest.com"],
            "startPublishedDate": "2026-01-01",
            "endPublishedDate": "2026-06-01",
            "category": "research paper",
            "userLocation": "US",
            "contents": {
                "text": {"maxCharacters": 2000},
                "subpages": 2,
                "maxAgeHours": 0,
            },
            "moderation": True,
        }
    )
    rest = req.to_rest_search_request()
    assert rest.search_depth == "fast"
    assert rest.max_results == 12
    assert rest.allowed_domains == ["arxiv.org"]
    assert rest.blocked_domains == ["pinterest.com"]
    assert rest.topic == "research paper"
    assert rest.country == "US"
    assert rest.include_content is True
    assert rest.format == "exa"

    assert (
        ExaSearchRequest(query="q", type="deep-reasoning")
        .to_rest_search_request()
        .search_depth
        == "deep"
    )
    assert (
        ExaSearchRequest(query="q", type="auto").to_rest_search_request().search_depth
        == "basic"
    )

    with pytest.raises(ValidationError):
        ExaSearchRequest.model_validate(
            {
                "query": "q",
                "startPublishedDate": "2026-05-01",
                "endPublishedDate": "2026-04-01",
            }
        )
    with pytest.raises(ValidationError):
        ExaSearchRequest.model_validate({"query": "q", "numResults": True})

    exa_payload = to_exa_search_payload(
        _sample_rest_response(with_metadata=True), resolved_search_type="lite"
    )
    assert exa_payload["requestId"] == "upstream-123"
    assert exa_payload["resolvedSearchType"] == "lite"
    assert exa_payload["costDollars"] == {"total": 0.005}
    row = exa_payload["results"][0]
    assert row["id"] == "ws_abc123:0"
    assert row["publishedDate"] == "2026-10-01"
    assert row["author"] == "Ada"
    assert row["score"] == 0.92
    assert row["text"] == "Full extracted markdown"
    assert row["highlights"] == ["Highlighted passage"]
    assert row["highlightScores"] == [0.88]

    contents_req = ExaContentsRequest.model_validate(
        {"ids": [" https://a.io "], "text": {"maxCharacters": 500}}
    )
    assert contents_req.normalized_urls() == ["https://a.io"]
    assert contents_req.effective_max_chars() == 500
    contents_payload = to_exa_contents_payload(
        [
            BatchFetchOutcome(url="https://a.io", title="A", content="Alpha"),
            BatchFetchOutcome(url="https://b.io", error="Blocked", status_code=400),
        ],
        request_id="req_exa",
    )
    assert contents_payload["results"] == [
        {"id": "https://a.io", "url": "https://a.io", "title": "A", "text": "Alpha"}
    ]
    assert contents_payload["statuses"][0] == {
        "id": "https://a.io",
        "status": "success",
    }
    assert contents_payload["statuses"][1]["status"] == "error"


def test_brave_deepseek_and_searxng_translators() -> None:
    brave_q = BraveSearchQuery.model_validate(
        {
            "q": " brave query ",
            "count": 4,
            "freshness": "2026-01-01to2026-01-15",
            "country": "gb",
            "search_lang": "en",
            "extra_snippets": True,
        }
    )
    brave_rest = brave_q.to_rest_search_request()
    assert brave_rest.query == "brave query"
    assert brave_rest.max_results == 4
    assert brave_rest.start_date == "2026-01-01"
    assert brave_rest.end_date == "2026-01-15"
    assert brave_rest.language == "en"
    assert brave_rest.include_content is True

    assert (
        BraveSearchQuery(q="q", freshness="pd").to_rest_search_request().time_range
        == "day"
    )
    for bad_brave in (
        {"q": ""},
        {"q": "ok", "freshness": "bad"},
        {"q": "ok", "freshness": "2026-02-01to2026-01-01"},
        {"q": "ok", "count": True},
    ):
        with pytest.raises(ValidationError):
            BraveSearchQuery.model_validate(bad_brave)

    brave_payload = to_brave_search_payload(_sample_rest_response(with_metadata=True))
    assert brave_payload["type"] == "search"
    assert brave_payload["query"] == {"original": "python 3.14"}
    assert brave_payload["web"]["results"][0]["extra_snippets"] == [
        "Highlighted passage"
    ]

    ds_q = DeepSeekSearchRequest(query="  ds query  ", count=6, need_content=True)
    assert ds_q.to_rest_search_request().include_content is True
    ds_payload_off = to_deepseek_search_payload(
        _sample_rest_response(with_metadata=True), need_content=False
    )
    assert ds_payload_off["results"][0]["content"] == ""
    assert ds_payload_off["results"][0]["authority"] is None
    ds_payload_on = to_deepseek_search_payload(
        _sample_rest_response(with_metadata=True), need_content=True
    )
    assert ds_payload_on["results"][0]["content"] == "Full extracted markdown"
    assert (
        DeepSeekFetchRequest(url=" https://x.io ").to_rest_fetch_request().url
        == "https://x.io"
    )
    assert to_deepseek_fetch_payload(
        WebFetchRestResponse(
            id="wf_1",
            url="https://x.io",
            title="X",
            media_type="text/plain",
            extractor_used="basic",
            truncated=False,
            content="Body",
        )
    ) == {"title": "X", "content": "Body"}

    # M3: SearXNG rejects non-JSON formats while ignoring multi-engine lists
    sx_q = SearxngSearchQuery.model_validate(
        {
            "q": " searx ",
            "format": "JSON",
            "engines": "google,bing",
            "categories": "it",
            "time_range": "month",
        }
    )
    assert sx_q.format == "json"
    assert sx_q.to_rest_search_request().time_range == "month"
    with pytest.raises(ValidationError, match="Only format=json is supported"):
        SearxngSearchQuery.model_validate({"q": "test", "format": "html"})

    sx_payload = to_searxng_search_payload(
        _sample_rest_response(with_metadata=True), category="it"
    )
    assert sx_payload["number_of_results"] == 1
    assert sx_payload["answers"] == ["Upstream answer"]
    assert sx_payload["results"][0]["category"] == "it"
    assert sx_payload["corrections"] == []


def test_iv16_missing_upstream_metadata_stays_none_across_all_dialects() -> None:
    """M6 guard: missing score/date/author/favicon/site_name/cost never fabricate defaults."""
    sparse = _sample_rest_response(with_metadata=False, with_synthesis=False)

    tavily = translate_websearch_response(sparse, "tavily")
    assert tavily["answer"] is None
    assert tavily["response_time"] is None
    assert tavily["usage"] == {"credits": None}
    assert tavily["results"][0]["score"] is None
    assert tavily["results"][0]["raw_content"] is None
    assert tavily["results"][0]["published_date"] is None
    assert tavily["results"][0]["favicon"] is None

    exa = translate_websearch_response(sparse, "exa")
    assert "costDollars" not in exa
    assert exa["results"][0]["score"] is None
    assert exa["results"][0]["publishedDate"] is None
    assert exa["results"][0]["author"] is None
    assert exa["results"][0]["highlightScores"] is None

    brave = translate_websearch_response(sparse, "brave")
    assert brave["web"]["results"][0]["page_age"] is None
    assert "extra_snippets" not in brave["web"]["results"][0]

    deepseek = translate_websearch_response(sparse, "deepseek", need_content=False)
    assert deepseek["results"][0]["rerank_score"] is None
    assert deepseek["results"][0]["site_name"] is None
    assert deepseek["results"][0]["site_icon"] is None
    assert deepseek["results"][0]["date"] is None
    assert deepseek["results"][0]["authority"] is None

    searxng = translate_websearch_response(sparse, "searxng")
    assert searxng["answers"] == []
    assert searxng["results"][0]["score"] is None
    assert searxng["results"][0]["publishedDate"] is None
