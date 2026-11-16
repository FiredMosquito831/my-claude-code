"""Pure dialect schemas and translators for Tavily, Exa, Brave, DeepSeek, and SearXNG.

Part of ``core``: stdlib and ``pydantic`` only (no HTTP transport or config imports).
All schemas are written natively in first-party MCC Python from documented wire
shapes with zero external copyleft/FSL code (WORKING-NOTES IX.9) and strictly
preserve nullable upstream fields without fabricating missing metadata (IV.16).
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .rest_models import (
    WebFetchRestRequest,
    WebFetchRestResponse,
    WebSearchRestRequest,
    WebSearchRestResponse,
    WebSearchRestSynthesis,
    _reject_bool_for_int,
    _strip_optional_text,
    _validate_iso_date_text,
)

SearchDialectName = Literal["tavily", "exa", "brave", "deepseek", "searxng"]

_TAVILY_DEPTH_MAP: dict[str, Literal["fast", "basic", "deep"]] = {
    "basic": "basic",
    "fast": "fast",
    "ultra-fast": "fast",
    "advanced": "deep",
}

_TAVILY_TIME_RANGE_MAP: dict[str, Literal["day", "week", "month", "year"]] = {
    "day": "day",
    "d": "day",
    "week": "week",
    "w": "week",
    "month": "month",
    "m": "month",
    "year": "year",
    "y": "year",
}

_EXA_TYPE_MAP: dict[str, Literal["fast", "basic", "deep"]] = {
    "instant": "fast",
    "lite": "fast",
    "fast": "fast",
    "keyword": "fast",
    "auto": "basic",
    "neural": "basic",
    "deep": "deep",
    "deep-lite": "deep",
    "deep-reasoning": "deep",
}

_BRAVE_FRESHNESS_MAP: dict[str, Literal["day", "week", "month", "year"]] = {
    "pd": "day",
    "pw": "week",
    "pm": "month",
    "py": "year",
}


def _strip_query_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


def _normalize_domain_list(values: list[str]) -> list[str]:
    return [entry.strip() for entry in values if entry.strip()]


def _normalize_provider_name(value: str | None) -> str | None:
    return value.lower() if value is not None else None


def _normalize_batch_urls(raw: str | list[str]) -> list[str]:
    candidates = [raw] if isinstance(raw, str) else list(raw)
    cleaned: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            raise ValueError("urls entries must be strings")
        stripped = item.strip()
        if not stripped:
            raise ValueError("urls entries must be non-empty")
        if len(stripped) > 8192:
            raise ValueError("url exceeds maximum length of 8192 characters")
        cleaned.append(stripped)
    if not cleaned:
        raise ValueError("urls must contain at least one URL")
    if len(cleaned) > 20:
        raise ValueError("urls supports at most 20 URLs per request")
    return cleaned


def _days_to_time_range(days: int) -> Literal["day", "week", "month", "year"]:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


def _effective_answer(response: WebSearchRestResponse) -> str | None:
    if isinstance(response.synthesis, WebSearchRestSynthesis):
        return response.synthesis.answer
    if isinstance(response.synthesis, dict):
        synth_answer = response.synthesis.get("answer")
        if isinstance(synth_answer, str):
            return synth_answer
    return response.answer


@dataclass(frozen=True, slots=True)
class BatchFetchOutcome:
    """Normalized outcome for one URL in a multi-URL extract/contents request."""

    url: str
    title: str = ""
    content: str = ""
    error: str | None = None
    status_code: int | None = None
    final_url: str | None = None


class TavilySearchRequest(BaseModel):
    """Incoming Tavily ``POST /search`` wire payload."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query: str = Field(..., min_length=1, max_length=2000)
    search_depth: Literal["basic", "fast", "ultra-fast", "advanced"] | None = None
    topic: str | None = Field(default=None, max_length=64)
    max_results: int | None = Field(default=None, ge=1, le=50)
    time_range: str | None = None
    days: int | None = Field(default=None, ge=1, le=3650)
    start_date: str | None = None
    end_date: str | None = None
    country: str | None = Field(default=None, max_length=64)
    include_answer: bool | Literal["basic", "advanced"] | None = None
    include_raw_content: bool | Literal["markdown", "text"] | None = None
    include_images: bool = False
    include_favicon: bool | None = None
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    provider: str | None = None
    api_key: str | None = None
    synthesize: bool = False
    model: str | None = None
    synthesis_max_tokens: int | None = Field(default=None, ge=128, le=8192)

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: Any) -> Any:
        return _strip_query_value(value)

    @field_validator(
        "topic",
        "time_range",
        "start_date",
        "end_date",
        "country",
        "provider",
        "api_key",
        "model",
        mode="before",
    )
    @classmethod
    def _normalize_optional(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("provider")
    @classmethod
    def _lowercase_provider(cls, value: str | None) -> str | None:
        return _normalize_provider_name(value)

    @field_validator("max_results", "days", "synthesis_max_tokens", mode="before")
    @classmethod
    def _validate_ints(cls, value: Any) -> Any:
        return _reject_bool_for_int(value)

    @field_validator("time_range")
    @classmethod
    def _validate_time_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        mapped = _TAVILY_TIME_RANGE_MAP.get(value.lower())
        if mapped is None:
            raise ValueError("time_range must be one of day, week, month, year")
        return mapped

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_dates(cls, value: str | None) -> str | None:
        return _validate_iso_date_text(value)

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def _clean_domains(cls, values: list[str]) -> list[str]:
        return _normalize_domain_list(values)

    @model_validator(mode="after")
    def _validate_date_order(self) -> TavilySearchRequest:
        if (
            self.start_date
            and self.end_date
            and self.start_date[:10] > self.end_date[:10]
        ):
            raise ValueError("start_date must not be after end_date")
        return self

    def to_rest_search_request(self) -> WebSearchRestRequest:
        """Translate this Tavily search payload into a canonical MCC REST request."""
        mapped_time_range: Literal["day", "week", "month", "year"] | None = None
        if self.time_range is not None:
            mapped_time_range = _TAVILY_TIME_RANGE_MAP[self.time_range]
        elif self.days is not None:
            mapped_time_range = _days_to_time_range(self.days)

        mapped_depth = (
            _TAVILY_DEPTH_MAP[self.search_depth]
            if self.search_depth is not None
            else None
        )
        include_answer: bool | None
        if isinstance(self.include_answer, str):
            include_answer = True
        else:
            include_answer = self.include_answer

        include_content: bool | None = None
        raw_content_format: Literal["markdown", "text"] | None = None
        if isinstance(self.include_raw_content, str):
            include_content = True
            raw_content_format = self.include_raw_content
        elif isinstance(self.include_raw_content, bool):
            include_content = self.include_raw_content

        return WebSearchRestRequest(
            query=self.query,
            provider=self.provider,
            max_results=self.max_results,
            allowed_domains=self.include_domains,
            blocked_domains=self.exclude_domains,
            time_range=mapped_time_range,
            start_date=self.start_date,
            end_date=self.end_date,
            search_depth=mapped_depth,
            topic=self.topic,
            country=self.country,
            include_content=include_content,
            include_answer=include_answer,
            raw_content_format=raw_content_format,
            synthesize=self.synthesize,
            model=self.model,
            synthesis_max_tokens=self.synthesis_max_tokens,
            format="tavily",
        )


class TavilyExtractRequest(BaseModel):
    """Incoming Tavily ``POST /extract`` wire payload."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    urls: str | list[str]
    format: Literal["markdown", "text"] | None = None
    extract_depth: Literal["basic", "advanced"] | None = None
    include_images: bool = False
    include_favicon: bool = False
    api_key: str | None = None

    @field_validator("api_key", mode="before")
    @classmethod
    def _normalize_api_key(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("urls")
    @classmethod
    def _validate_urls(cls, value: str | list[str]) -> list[str]:
        return _normalize_batch_urls(value)

    def normalized_urls(self) -> list[str]:
        return list(self.urls) if isinstance(self.urls, list) else [self.urls]

    def effective_extractor(self) -> Literal["trafilatura", "basic"] | None:
        if self.format == "text":
            return "basic"
        if self.format == "markdown":
            return "trafilatura"
        return None


def to_tavily_search_payload(
    response: WebSearchRestResponse,
    *,
    include_images: bool = False,
) -> dict[str, Any]:
    """Serialize a canonical search response into Tavily ``/search`` JSON."""
    images: list[str | dict[str, Any]] = []
    if include_images and response.images:
        images = list(response.images)
    response_time = (
        round(response.provider_response_time_ms / 1000.0, 3)
        if response.provider_response_time_ms is not None
        else None
    )
    return {
        "query": response.query,
        "answer": _effective_answer(response),
        "images": images,
        "results": [
            {
                "title": hit.title,
                "url": hit.url,
                "content": hit.snippet or hit.content or "",
                "score": hit.score,
                "raw_content": hit.content,
                "published_date": hit.published,
                "favicon": hit.favicon,
            }
            for hit in response.results
        ],
        "response_time": response_time,
        "usage": {"credits": response.usage.usage_credits},
        "request_id": response.request_id or response.id,
    }


def to_tavily_extract_payload(
    outcomes: list[BatchFetchOutcome],
    *,
    request_id: str,
    elapsed_ms: float,
) -> dict[str, Any]:
    """Serialize per-URL fetch outcomes into Tavily ``/extract`` JSON."""
    results: list[dict[str, Any]] = []
    failed_results: list[dict[str, str]] = []
    for item in outcomes:
        if item.error is not None:
            failed_results.append({"url": item.url, "error": item.error})
        else:
            results.append(
                {
                    "url": item.final_url or item.url,
                    "raw_content": item.content,
                    "title": item.title,
                }
            )
    return {
        "results": results,
        "failed_results": failed_results,
        "response_time": round(elapsed_ms / 1000.0, 3),
        "request_id": request_id,
    }


class ExaContentsOptions(BaseModel):
    """Nested ``contents`` block accepted by Exa ``POST /search``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    text: bool | dict[str, Any] | None = None
    highlights: bool | dict[str, Any] | None = None
    summary: bool | dict[str, Any] | None = None

    def resolve_include_content(self) -> bool | None:
        if bool(self.text or self.highlights or self.summary):
            return True
        if any(field is False for field in (self.text, self.highlights, self.summary)):
            return False
        return None


class ExaSearchRequest(BaseModel):
    """Incoming Exa ``POST /search`` wire payload."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query: str = Field(..., min_length=1, max_length=2000)
    type: str | None = None
    numResults: int | None = Field(default=None, ge=1, le=50)
    includeDomains: list[str] = Field(default_factory=list)
    excludeDomains: list[str] = Field(default_factory=list)
    startPublishedDate: str | None = None
    endPublishedDate: str | None = None
    category: str | None = Field(default=None, max_length=64)
    userLocation: str | None = Field(default=None, max_length=64)
    contents: ExaContentsOptions | bool | None = None
    provider: str | None = None
    synthesize: bool = False
    model: str | None = None
    synthesis_max_tokens: int | None = Field(default=None, ge=128, le=8192)

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: Any) -> Any:
        return _strip_query_value(value)

    @field_validator(
        "type",
        "startPublishedDate",
        "endPublishedDate",
        "category",
        "userLocation",
        "provider",
        "model",
        mode="before",
    )
    @classmethod
    def _normalize_optional(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("provider")
    @classmethod
    def _lowercase_provider(cls, value: str | None) -> str | None:
        return _normalize_provider_name(value)

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if normalized not in _EXA_TYPE_MAP:
            raise ValueError(
                "type must be one of auto, lite, instant, fast, "
                "deep-lite, deep, deep-reasoning, neural, keyword"
            )
        return normalized

    @field_validator("numResults", "synthesis_max_tokens", mode="before")
    @classmethod
    def _validate_ints(cls, value: Any) -> Any:
        return _reject_bool_for_int(value)

    @field_validator("startPublishedDate", "endPublishedDate")
    @classmethod
    def _validate_dates(cls, value: str | None) -> str | None:
        return _validate_iso_date_text(value)

    @field_validator("includeDomains", "excludeDomains")
    @classmethod
    def _clean_domains(cls, values: list[str]) -> list[str]:
        return _normalize_domain_list(values)

    @model_validator(mode="after")
    def _validate_date_order(self) -> ExaSearchRequest:
        if (
            self.startPublishedDate
            and self.endPublishedDate
            and self.startPublishedDate[:10] > self.endPublishedDate[:10]
        ):
            raise ValueError("startPublishedDate must not be after endPublishedDate")
        return self

    def to_rest_search_request(self) -> WebSearchRestRequest:
        """Translate this Exa search payload into a canonical MCC REST request."""
        mapped_depth = _EXA_TYPE_MAP[self.type] if self.type is not None else None
        include_content: bool | None = None
        if isinstance(self.contents, bool):
            include_content = self.contents
        elif isinstance(self.contents, ExaContentsOptions):
            include_content = self.contents.resolve_include_content()

        return WebSearchRestRequest(
            query=self.query,
            provider=self.provider,
            max_results=self.numResults,
            allowed_domains=self.includeDomains,
            blocked_domains=self.excludeDomains,
            start_date=self.startPublishedDate,
            end_date=self.endPublishedDate,
            search_depth=mapped_depth,
            topic=self.category,
            country=self.userLocation,
            include_content=include_content,
            synthesize=self.synthesize,
            model=self.model,
            synthesis_max_tokens=self.synthesis_max_tokens,
            format="exa",
        )


class ExaContentsRequest(BaseModel):
    """Incoming Exa ``POST /contents`` wire payload."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    urls: str | list[str] = Field(
        validation_alias=AliasChoices("urls", "ids"),
    )
    text: bool | dict[str, Any] | None = None
    highlights: bool | dict[str, Any] | None = None
    summary: bool | dict[str, Any] | None = None

    @field_validator("urls")
    @classmethod
    def _validate_urls(cls, value: str | list[str]) -> list[str]:
        return _normalize_batch_urls(value)

    def normalized_urls(self) -> list[str]:
        return list(self.urls) if isinstance(self.urls, list) else [self.urls]

    def effective_max_chars(self) -> int | None:
        if isinstance(self.text, dict):
            raw = self.text.get("maxCharacters")
            if not isinstance(raw, bool) and isinstance(raw, int):
                return max(200, min(raw, 200_000))
        return None


def to_exa_search_payload(
    response: WebSearchRestResponse,
    *,
    resolved_search_type: str | None = None,
) -> dict[str, Any]:
    """Serialize a canonical search response into Exa ``/search`` JSON."""
    payload: dict[str, Any] = {
        "requestId": response.request_id or response.id,
        "resolvedSearchType": resolved_search_type,
        "results": [
            {
                "id": f"{response.id}:{idx}",
                "url": hit.url,
                "title": hit.title,
                "publishedDate": hit.published,
                "author": hit.author,
                "score": hit.score,
                "text": hit.content,
                "highlights": hit.highlights,
                "highlightScores": hit.highlight_scores,
                "favicon": hit.favicon,
            }
            for idx, hit in enumerate(response.results)
        ],
    }
    if response.usage.search_cost_usd is not None:
        payload["costDollars"] = {"total": response.usage.search_cost_usd}
    if response.synthesis is not None:
        synth_answer = _effective_answer(response)
        if synth_answer is not None:
            payload["answer"] = synth_answer
    return payload


def to_exa_contents_payload(
    outcomes: list[BatchFetchOutcome],
    *,
    request_id: str,
) -> dict[str, Any]:
    """Serialize per-URL fetch outcomes into Exa ``/contents`` JSON."""
    results: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for item in outcomes:
        if item.error is not None:
            error_entry: dict[str, Any] = {"tag": "CRAWL_ERROR", "message": item.error}
            if item.status_code is not None:
                error_entry["httpStatusCode"] = item.status_code
            statuses.append(
                {
                    "id": item.url,
                    "status": "error",
                    "error": error_entry,
                }
            )
        else:
            results.append(
                {
                    "id": item.url,
                    "url": item.final_url or item.url,
                    "title": item.title,
                    "text": item.content,
                }
            )
            statuses.append({"id": item.url, "status": "success"})
    return {
        "requestId": request_id,
        "results": results,
        "statuses": statuses,
    }


class BraveSearchQuery(BaseModel):
    """Validated query parameters for Brave ``GET /res/v1/web/search`` compatibility."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    q: str = Field(..., min_length=1, max_length=2000)
    count: int | None = Field(default=None, ge=1, le=50)
    freshness: str | None = None
    country: str | None = Field(default=None, max_length=64)
    search_lang: str | None = Field(default=None, max_length=32)
    extra_snippets: bool | None = None
    provider: str | None = None

    @field_validator("q", mode="before")
    @classmethod
    def _normalize_query(cls, value: Any) -> Any:
        return _strip_query_value(value)

    @field_validator("freshness", "country", "search_lang", "provider", mode="before")
    @classmethod
    def _normalize_optional(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("provider")
    @classmethod
    def _lowercase_provider(cls, value: str | None) -> str | None:
        return _normalize_provider_name(value)

    @field_validator("count", mode="before")
    @classmethod
    def _validate_count(cls, value: Any) -> Any:
        return _reject_bool_for_int(value)

    @field_validator("freshness")
    @classmethod
    def _validate_freshness(cls, value: str | None) -> str | None:
        if value is None:
            return None
        lowered = value.lower()
        if lowered in _BRAVE_FRESHNESS_MAP:
            return lowered
        if "to" in value:
            parts = value.split("to")
            if len(parts) == 2 and len(parts[0]) == 10 and len(parts[1]) == 10:
                start = _validate_iso_date_text(parts[0])
                end = _validate_iso_date_text(parts[1])
                if start and end and start > end:
                    raise ValueError("freshness start date must not be after end date")
                return value
        raise ValueError(
            "freshness must be one of pd, pw, pm, py, or YYYY-MM-DDtoYYYY-MM-DD"
        )

    def to_rest_search_request(self) -> WebSearchRestRequest:
        """Translate Brave query parameters into a canonical MCC REST request."""
        time_range: Literal["day", "week", "month", "year"] | None = None
        start_date: str | None = None
        end_date: str | None = None
        if self.freshness is not None:
            if self.freshness in _BRAVE_FRESHNESS_MAP:
                time_range = _BRAVE_FRESHNESS_MAP[self.freshness]
            else:
                start_date, end_date = self.freshness.split("to", 1)

        return WebSearchRestRequest(
            query=self.q,
            provider=self.provider,
            max_results=self.count,
            time_range=time_range,
            start_date=start_date,
            end_date=end_date,
            country=self.country,
            language=self.search_lang,
            include_content=self.extra_snippets,
            format="brave",
        )


def to_brave_search_payload(response: WebSearchRestResponse) -> dict[str, Any]:
    """Serialize a canonical search response into Brave ``/res/v1/web/search`` JSON."""
    results: list[dict[str, Any]] = []
    for hit in response.results:
        item: dict[str, Any] = {
            "title": hit.title,
            "url": hit.url,
            "description": hit.snippet or hit.content or "",
            "page_age": hit.published,
        }
        if hit.highlights:
            item["extra_snippets"] = list(hit.highlights)
        results.append(item)
    return {
        "type": "search",
        "query": {"original": response.query},
        "web": {"results": results},
    }


class DeepSeekSearchRequest(BaseModel):
    """Incoming DeepSeek ``POST /v1/tools/search`` wire payload."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query: str = Field(..., min_length=1, max_length=2000)
    count: int | None = Field(default=None, ge=1, le=50)
    ori_question: str | None = None
    localization: str | None = Field(default=None, max_length=64)
    need_content: bool = False
    provider: str | None = None
    synthesize: bool = False
    model: str | None = None
    synthesis_max_tokens: int | None = Field(default=None, ge=128, le=8192)

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: Any) -> Any:
        return _strip_query_value(value)

    @field_validator(
        "ori_question",
        "localization",
        "provider",
        "model",
        mode="before",
    )
    @classmethod
    def _normalize_optional(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("provider")
    @classmethod
    def _lowercase_provider(cls, value: str | None) -> str | None:
        return _normalize_provider_name(value)

    @field_validator("count", "synthesis_max_tokens", mode="before")
    @classmethod
    def _validate_ints(cls, value: Any) -> Any:
        return _reject_bool_for_int(value)

    def to_rest_search_request(self) -> WebSearchRestRequest:
        """Translate a DeepSeek search request into a canonical MCC REST request."""
        return WebSearchRestRequest(
            query=self.query,
            provider=self.provider,
            max_results=self.count,
            include_content=True if self.need_content else None,
            synthesize=self.synthesize,
            model=self.model,
            synthesis_max_tokens=self.synthesis_max_tokens,
            format="deepseek",
        )


class DeepSeekFetchRequest(BaseModel):
    """Incoming DeepSeek ``POST /v1/tools/fetch`` wire payload."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    url: str = Field(..., min_length=1, max_length=8192)

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: Any) -> Any:
        return _strip_query_value(value)

    def to_rest_fetch_request(self) -> WebFetchRestRequest:
        return WebFetchRestRequest(url=self.url)


def to_deepseek_search_payload(
    response: WebSearchRestResponse,
    *,
    need_content: bool = False,
) -> dict[str, Any]:
    """Serialize a canonical search response into DeepSeek ``/v1/tools/search`` JSON."""
    return {
        "results": [
            {
                "rerank_score": hit.score,
                "title": hit.title,
                "url": hit.url,
                "snippet": hit.snippet,
                "summary": hit.snippet,
                "content": (hit.content or "") if need_content else "",
                "site_name": hit.site_name,
                "site_icon": hit.favicon,
                "date": hit.published,
                "authority": None,
            }
            for hit in response.results
        ]
    }


def to_deepseek_fetch_payload(response: WebFetchRestResponse) -> dict[str, Any]:
    """Serialize a canonical fetch response into DeepSeek ``/v1/tools/fetch`` JSON."""
    return {
        "title": response.title,
        "content": response.content,
    }


class SearxngSearchQuery(BaseModel):
    """Validated query parameters for SearXNG JSON ``GET /search?format=json``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    q: str = Field(..., min_length=1, max_length=2000)
    format: str | None = "json"
    time_range: Literal["day", "week", "month", "year"] | None = None
    language: str | None = Field(default=None, max_length=32)
    categories: str | None = None
    engines: str | None = None
    pageno: int = Field(default=1, ge=1, le=100)
    max_results: int | None = Field(default=None, ge=1, le=50)
    provider: str | None = None

    @field_validator("q", mode="before")
    @classmethod
    def _normalize_query(cls, value: Any) -> Any:
        return _strip_query_value(value)

    @field_validator(
        "format",
        "language",
        "categories",
        "engines",
        "provider",
        mode="before",
    )
    @classmethod
    def _normalize_optional(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("provider")
    @classmethod
    def _lowercase_provider(cls, value: str | None) -> str | None:
        return _normalize_provider_name(value)

    @field_validator("format")
    @classmethod
    def _validate_json_format(cls, value: str | None) -> str:
        effective = (value or "json").lower()
        if effective != "json":
            raise ValueError("Only format=json is supported")
        return "json"

    @field_validator("pageno", "max_results", mode="before")
    @classmethod
    def _validate_ints(cls, value: Any) -> Any:
        return _reject_bool_for_int(value)

    def to_rest_search_request(self) -> WebSearchRestRequest:
        """Translate SearXNG JSON query parameters into a canonical MCC REST request."""
        return WebSearchRestRequest(
            query=self.q,
            provider=self.provider,
            max_results=self.max_results,
            time_range=self.time_range,
            language=self.language,
            format="searxng",
        )


def to_searxng_search_payload(
    response: WebSearchRestResponse,
    *,
    category: str | None = None,
) -> dict[str, Any]:
    """Serialize a canonical search response into SearXNG ``format=json`` shape."""
    effective_category = (
        category.strip()
        if isinstance(category, str) and category.strip()
        else "general"
    )
    answer = _effective_answer(response)
    return {
        "query": response.query,
        "number_of_results": len(response.results),
        "results": [
            {
                "url": hit.url,
                "title": hit.title,
                "content": hit.snippet or hit.content or "",
                "engine": response.provider,
                "engines": [response.provider],
                "score": hit.score,
                "publishedDate": hit.published,
                "category": effective_category,
            }
            for hit in response.results
        ],
        "answers": [answer] if answer else [],
        "corrections": [],
        "infoboxes": [],
        "suggestions": [],
        "unresponsive_engines": [],
    }


def translate_websearch_response(
    response: WebSearchRestResponse,
    dialect: SearchDialectName,
    *,
    include_images: bool = False,
    resolved_search_type: str | None = None,
    need_content: bool = False,
    category: str | None = None,
) -> dict[str, Any]:
    """Translate a canonical ``WebSearchRestResponse`` to the requested wire dialect."""
    if dialect == "tavily":
        return to_tavily_search_payload(response, include_images=include_images)
    if dialect == "exa":
        return to_exa_search_payload(
            response, resolved_search_type=resolved_search_type
        )
    if dialect == "brave":
        return to_brave_search_payload(response)
    if dialect == "deepseek":
        return to_deepseek_search_payload(response, need_content=need_content)
    return to_searxng_search_payload(response, category=category)
