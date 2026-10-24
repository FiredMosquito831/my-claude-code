"""Web search provider adapter registry (catalog-order ``ADAPTER_CLASSES``)."""

from ..base import BaseWebSearchProvider
from .brave import BraveWebSearchProvider
from .context7 import Context7WebSearchProvider
from .ddgs import DdgsWebSearchProvider
from .exa import ExaWebSearchProvider
from .firecrawl import FirecrawlWebSearchProvider
from .jina import JinaWebSearchProvider
from .linkup import LinkupWebSearchProvider
from .ollama import OllamaWebSearchProvider
from .parallel import ParallelWebSearchProvider
from .perplexity import PerplexityWebSearchProvider
from .searchapi import SearchApiWebSearchProvider
from .searxng import SearxngWebSearchProvider
from .serpapi import SerpApiWebSearchProvider
from .serper import SerperWebSearchProvider
from .tavily import TavilyWebSearchProvider

# Insertion order mirrors WEBSEARCH_CATALOG (contract-tested).
ADAPTER_CLASSES: dict[str, type[BaseWebSearchProvider]] = {
    "ddgs": DdgsWebSearchProvider,
    "ollama": OllamaWebSearchProvider,
    "exa": ExaWebSearchProvider,
    "tavily": TavilyWebSearchProvider,
    "brave": BraveWebSearchProvider,
    "searxng": SearxngWebSearchProvider,
    "jina": JinaWebSearchProvider,
    "serper": SerperWebSearchProvider,
    "firecrawl": FirecrawlWebSearchProvider,
    "linkup": LinkupWebSearchProvider,
    "perplexity": PerplexityWebSearchProvider,
    "parallel": ParallelWebSearchProvider,
    "searchapi": SearchApiWebSearchProvider,
    "serpapi": SerpApiWebSearchProvider,
    "context7": Context7WebSearchProvider,
}

__all__ = ["ADAPTER_CLASSES"]
