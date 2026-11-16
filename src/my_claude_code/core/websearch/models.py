"""Neutral web search result contracts shared by adapters and integrations.

This module is part of ``core``: stdlib-only, no transport (httpx/aiohttp) and
no ``config`` imports (see contract tests).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class WebSearchResultItem:
    """One normalized search result independent of provider wire format."""

    title: str
    url: str
    snippet: str  # "" when absent
    content: str | None  # fuller text when the provider returns it
    published: str | None  # ISO date when known
    score: float | None = None
    author: str | None = None
    favicon: str | None = None
    site_name: str | None = None
    highlights: tuple[str, ...] | None = None
    highlight_scores: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class WebSearchResponse:
    """Normalized response for one web search call."""

    provider: str  # provider_id
    query: str
    results: tuple[WebSearchResultItem, ...]
    key_index: int  # which key served it (0-based)
    cost_usd: float | None
    answer: str | None = None  # provider LLM answer / rich block lead when present
    request_id: str | None = None
    provider_response_time_ms: float | None = None
    usage_credits: float | None = None
    images: tuple[str | dict[str, Any], ...] | None = None


@dataclass(frozen=True, slots=True)
class WebSearchRequestOptions:
    """Per-request search overrides carried without mutating adapter config."""

    time_range: Literal["day", "week", "month", "year"] | None = None
    start_date: str | None = None
    end_date: str | None = None
    search_depth: Literal["fast", "basic", "deep"] | None = None
    topic: str | None = None
    country: str | None = None
    language: str | None = None
    include_content: bool | None = None
    include_answer: bool | None = None
    raw_content_format: Literal["markdown", "text"] | None = None
    provider_params: Mapping[str, Any] | None = None

    def non_empty_dict(self) -> dict[str, Any]:
        """Return non-None override fields for diagnostics and search logging."""
        payload: dict[str, Any] = {}
        for field_name in (
            "time_range",
            "start_date",
            "end_date",
            "search_depth",
            "topic",
            "country",
            "language",
            "include_content",
            "include_answer",
            "raw_content_format",
            "provider_params",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            if field_name == "provider_params" and isinstance(value, Mapping):
                payload[field_name] = dict(value)
            else:
                payload[field_name] = value
        return payload
