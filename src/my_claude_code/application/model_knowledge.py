"""Everything known about one model: the vocabulary of the view (7.86.0).

The metadata ladder reads each field off one rung -- the provider's own list,
OpenRouter's live list, models.dev's bucket, its OpenRouter copy, the
cross-provider vote, LiteLLM's map, a learned fact, an override, the vendor's
own client -- and keeps only the winner. The Models page's "Everything known"
view shows every rung's statement and every source's own row beside the value
the ladder used, loaded on demand for one model at a time.

This module is the shape those rows travel in between the runtime, which reads
the stored files, and the admin page, which assembles the view. No I/O, and
no consumer: nothing routes, lists, prices or caches on any of it.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The provider's own list row, as stored beside the catalogue.
ROW_SOURCE_PROVIDER = "provider_list"
#: OpenRouter's live list, the row(s) the asking id met.
ROW_SOURCE_OPENROUTER_LIVE = "openrouter_live"
#: LiteLLM's map, the entries pricing's own walk meets (LiteLLM pricing on).
ROW_SOURCE_LITELLM = "litellm"


@dataclass(frozen=True, slots=True)
class PublishedRow:
    """One source's own row about one model, verbatim.

    ``key`` is the id the source files the row under (the provider's model
    id, an OpenRouter id, a LiteLLM key); ``match`` says how the asking id met
    it; ``as_of`` is when the source's copy was fetched or stored, ISO-8601.
    """

    source: str
    key: str
    row: Mapping[str, Any]
    match: str | None = None
    as_of: str | None = None


type PublishedRowsLookup = Callable[[str, str, Sequence[str]], tuple[PublishedRow, ...]]


__all__ = [
    "ROW_SOURCE_LITELLM",
    "ROW_SOURCE_OPENROUTER_LIVE",
    "ROW_SOURCE_PROVIDER",
    "PublishedRow",
    "PublishedRowsLookup",
]
