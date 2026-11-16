"""Neutral web search contracts (no transport or config dependencies)."""

from .models import WebSearchRequestOptions, WebSearchResponse, WebSearchResultItem
from .rest_models import (
    WebFetchRestRequest,
    WebFetchRestResponse,
    WebSearchRestRequest,
    WebSearchRestResponse,
    WebSearchRestResult,
    WebSearchRestUsage,
)

__all__ = [
    "WebFetchRestRequest",
    "WebFetchRestResponse",
    "WebSearchRequestOptions",
    "WebSearchResponse",
    "WebSearchRestRequest",
    "WebSearchRestResponse",
    "WebSearchRestResult",
    "WebSearchRestUsage",
    "WebSearchResultItem",
]
