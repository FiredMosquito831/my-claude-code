"""LiteLLM's model map as one more rung of the kind and display ladders (7.85.0).

LiteLLM's ``model_prices_and_context_window.json`` -- the second price source,
behind ``COST_SOURCE_LITELLM_ENABLED`` and off by default -- states more than
prices for the 3,849 entries it carries: what each model accepts and produces
(``supported_modalities`` / ``supported_output_modalities``, 465 entries with
both), its ``mode`` (``chat``, ``image_generation``, ``audio_transcription``
...), the endpoints that serve it (``supported_endpoints``) and the day LiteLLM
expects it to be retired (``deprecation_date``).

This module is the rung's vocabulary and nothing else -- no I/O:
``providers/runtime/litellm_prices.py`` reads the file and matches an entry
exactly as it does for pricing (a key naming the routed provider first, then a
bare key only where its own ``litellm_provider`` agrees). Every consumer reads
it only while LiteLLM pricing is on and its file is on disk; a consumer handed
``None`` produces exactly what it produced before 7.85.0.

Where it sits, spec §4.3 and §4.7, the same slot as pricing (below the
provider's own list and the models.dev catalogues, above the cross-provider
vote):

* **kind, fine (3b):** its modality pair, where every source above the vote is
  silent -- the provider's list, OpenRouter's live list, a models.dev bucket
  or models.dev's OpenRouter copy -- and above the vote;
* **kind, coarse (5b):** its ``mode`` and endpoint words, after the provider's
  own type and endpoint words, before the media rail you saved a model on;
* **endpoints shown:** its endpoint words where the provider's list names none;
* **retirement:** its ``deprecation_date``, after the vendor's own client and
  the provider's list, before models.dev's ``deprecated`` flag.

**Never routing, never cost.** Pricing reads LiteLLM through its own rate-card
walk, unchanged; nothing here feeds a request.
"""

from collections.abc import Callable
from dataclasses import dataclass

from my_claude_code.application.model_metadata import DeclaredModalities

#: The badge a value from this rung carries on the Models page.
LITELLM_SOURCE = "litellm"
LITELLM_SOURCE_LABEL = "LiteLLM model map"


@dataclass(frozen=True, slots=True)
class LiteLLMStatement[T]:
    """One thing LiteLLM states about a model, and the entry that stated it."""

    value: T
    #: The LiteLLM key that stated it (``novita/deepseek/deepseek-v3``).
    key: str
    #: How that key met the routed id, in the pricing walk's own words
    #: (``prefixed key``, ``cross_provider_exact`` ...).
    match: str

    @property
    def tier_label(self) -> str:
        """``"LiteLLM, prefixed key (novita/x)"``: the badge's rung line."""

        return f"LiteLLM, {self.match} ({self.key})"


@dataclass(frozen=True, slots=True)
class LiteLLMModel:
    """What LiteLLM's map states about one routed model. ``None`` = not stated.

    Each field comes from the first matching entry that states it, in the
    pricing walk's order. ``modalities`` holds both halves from ONE entry or
    nothing; ``words`` is one entry's ``mode`` followed by its endpoints, the
    coarse kind statement, exactly as the provider's own type-then-endpoints
    words are read.
    """

    modalities: LiteLLMStatement[DeclaredModalities] | None = None
    words: LiteLLMStatement[tuple[str, ...]] | None = None
    endpoints: LiteLLMStatement[tuple[str, ...]] | None = None
    deprecation_date: LiteLLMStatement[str] | None = None


type LiteLLMLookup = Callable[[str, str], LiteLLMModel | None]


@dataclass(frozen=True, slots=True)
class LiteLLMCatalogue:
    """One bound read of LiteLLM's stored map, like ``LiveCatalogue``.

    ``mark`` is the file's identity (``mtime_ns:size``), for a cache keyed on
    what a page was computed from.
    """

    mark: str
    lookup: LiteLLMLookup

    def __call__(self, provider_id: str, model_id: str) -> LiteLLMModel | None:
        return self.lookup(provider_id, model_id)


__all__ = [
    "LITELLM_SOURCE",
    "LITELLM_SOURCE_LABEL",
    "LiteLLMCatalogue",
    "LiteLLMLookup",
    "LiteLLMModel",
    "LiteLLMStatement",
]
