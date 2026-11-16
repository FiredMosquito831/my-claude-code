"""Pure Pydantic wire schemas for standalone REST websearch and webfetch routes.

Part of ``core``: stdlib and ``pydantic`` only (no HTTP transport or config imports).
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .models import WebSearchRequestOptions


def _reject_bool_for_int(value: Any) -> Any:
    """Prevent JSON booleans from silently coercing to integer counts."""
    if isinstance(value, bool):
        raise ValueError("expected an integer, not a boolean")
    return value


def _strip_optional_text(value: Any) -> Any:
    """Strip optional string inputs and normalize blank strings to None."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _validate_iso_date_text(value: str | None) -> str | None:
    """Validate optional ISO date (YYYY-MM-DD) or ISO-8601 datetime string."""
    if value is None:
        return None
    if len(value) == 10:
        date.fromisoformat(value)
        return value
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    datetime.fromisoformat(normalized)
    return value


class WebSearchRestRequest(BaseModel):
    """Canonical REST request body for ``POST /v1/websearch``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    query: str = Field(..., min_length=1, max_length=2000)
    provider: str | None = None
    max_results: int | None = Field(default=None, ge=1, le=50)
    allowed_domains: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("allowed_domains", "include_domains"),
    )
    blocked_domains: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("blocked_domains", "exclude_domains"),
    )
    time_range: Literal["day", "week", "month", "year"] | None = None
    start_date: str | None = None
    end_date: str | None = None
    search_depth: Literal["fast", "basic", "deep"] | None = None
    topic: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=32)
    include_content: bool | None = None
    fetch_missing_content: bool = False
    fetch_top_n: int = Field(default=3, ge=1, le=10)
    include_answer: bool | None = None
    include_digest: bool = False
    raw_content_format: Literal["markdown", "text"] | None = None
    format: Literal["mcc", "digest"] = "mcc"

    @field_validator("query", mode="before")
    @classmethod
    def _normalize_query(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator(
        "provider",
        "topic",
        "country",
        "language",
        "start_date",
        "end_date",
        mode="before",
    )
    @classmethod
    def _normalize_optional_strings(cls, value: Any) -> Any:
        return _strip_optional_text(value)

    @field_validator("provider")
    @classmethod
    def _lowercase_provider(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("max_results", "fetch_top_n", mode="before")
    @classmethod
    def _validate_int_fields(cls, value: Any) -> Any:
        return _reject_bool_for_int(value)

    @field_validator("allowed_domains", "blocked_domains")
    @classmethod
    def _clean_domains(cls, values: list[str]) -> list[str]:
        return [entry.strip() for entry in values if entry.strip()]

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_dates(cls, value: str | None) -> str | None:
        return _validate_iso_date_text(value)

    @model_validator(mode="after")
    def _validate_date_order(self) -> WebSearchRestRequest:
        if (
            self.start_date
            and self.end_date
            and self.start_date[:10] > self.end_date[:10]
        ):
            raise ValueError("start_date must not be after end_date")
        return self

    def to_request_options(self) -> WebSearchRequestOptions | None:
        """Build per-request adapter overrides or ``None`` when all overrides are unset."""
        values = (
            self.time_range,
            self.start_date,
            self.end_date,
            self.search_depth,
            self.topic,
            self.country,
            self.language,
            self.include_content,
            self.include_answer,
            self.raw_content_format,
        )
        if all(item is None for item in values):
            return None
        return WebSearchRequestOptions(
            time_range=self.time_range,
            start_date=self.start_date,
            end_date=self.end_date,
            search_depth=self.search_depth,
            topic=self.topic,
            country=self.country,
            language=self.language,
            include_content=self.include_content,
            include_answer=self.include_answer,
            raw_content_format=self.raw_content_format,
        )


class WebSearchRestResult(BaseModel):
    """Serialized search hit inside ``WebSearchRestResponse``."""

    title: str
    url: str
    snippet: str
    content: str | None = None
    published: str | None = None
    score: float | None = None
    author: str | None = None
    favicon: str | None = None
    site_name: str | None = None
    highlights: list[str] | None = None


class WebSearchRestUsage(BaseModel):
    """Usage and cost telemetry reported on ``POST /v1/websearch``."""

    search_requests: int = 1
    search_cost_usd: float | None = None
    usage_credits: float | None = None
    model_tokens_in: int | None = None
    model_tokens_out: int | None = None
    model_cost_usd: float | None = None


class WebSearchRestResponse(BaseModel):
    """Canonical JSON response returned by ``POST /v1/websearch``."""

    object: Literal["web_search"] = "web_search"
    id: str
    query: str
    provider: str
    fallback_used: bool
    providers_tried: list[str]
    supports_domain_filters: bool
    answer: str | None = None
    synthesis: dict[str, Any] | None = None
    results: list[WebSearchRestResult]
    digest: str | None = None
    provider_response_time_ms: float | None = None
    request_id: str | None = None
    usage: WebSearchRestUsage


class WebFetchRestRequest(BaseModel):
    """Canonical REST request body for ``POST /v1/webfetch``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str = Field(..., min_length=1, max_length=8192)
    extractor: Literal["trafilatura", "basic"] | None = None
    max_chars: int | None = Field(default=None, ge=200, le=200_000)
    allow_private_networks: bool | None = None

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("max_chars", mode="before")
    @classmethod
    def _validate_max_chars(cls, value: Any) -> Any:
        return _reject_bool_for_int(value)


class WebFetchRestResponse(BaseModel):
    """Canonical JSON response returned by ``POST /v1/webfetch``."""

    object: Literal["web_fetch"] = "web_fetch"
    id: str
    url: str
    title: str
    media_type: str
    extractor_used: str
    truncated: bool
    content: str
