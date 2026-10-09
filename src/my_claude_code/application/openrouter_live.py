"""OpenRouter's own live model list as one rung of the metadata ladder (7.84.0).

OpenRouter publishes, keyless, what each model it serves accepts and produces,
whether it reasons and takes tools, its window, its top deployment's output
limit, its prices, a description, a knowledge cutoff and the day OpenRouter
listed it. models.dev keeps a curated copy of the same publisher (the
"OpenRouter catalogue" rung, tiers 5-6), and MCC's own ``open_router``
provider reads the same list for its own models. Until 7.84.0 no OTHER provider
could read it: a Nous Portal, Novita or Command Code model that OpenRouter also
serves was described only by models.dev.

This module is the rung's vocabulary and its placement rule, and nothing else:
no I/O (``providers/runtime/openrouter_catalogue.py`` fetches, stores, matches
and binds it) and no knowledge of any one consumer. The user's binding
decisions (2026-10-08 21:00 and 21:33) fix the placement per field class:

* **Intrinsic fields** -- what the model accepts and produces (so its kind),
  image input, whether it reasons, tool calls. For a provider WITHOUT a
  models.dev bucket: the provider's own list, then **OpenRouter live**, then
  models.dev's copy (tiers 5-6) and the cross-provider vote (7-10). For a
  provider WITH a bucket: the provider's own list, then its bucket (3-4), then
  OpenRouter live only where both said nothing -- OpenRouter never overrides
  a bucket, and where the two disagree both statements are shown.
  :func:`live_wins_intrinsic` is that rule, stated once on tiers: a bucketed
  provider's models.dev answer is always tier 3-4 (a provider with a bucket
  never reads outside it), so "the existing answer is tier 5 or looser, or
  there is none" is exactly "bucket-less, or a gap".
* **Provider-specific numbers** -- window, output limit, prices. The provider,
  then models.dev, then OpenRouter LAST, gap filling only
  (:func:`live_fills_gap`). OpenRouter's output limit is its own top
  deployment's, which agrees with a provider's own bucket only 45 % of the
  time.
* **New display facts** -- description, knowledge cutoff, and the day
  OpenRouter LISTED the model. That date is never a release date and is
  never merged with one (``listed_at``).

**Never routing.** Nothing a request is built from reads this rung: not the
output-token clamp, not the context headroom, not reasoning gating, not vision
routing (user decision Q5, 2026-10-08 21:33). It reaches the Models page, the
kind lists (hide-only) and the agent catalogues, each at its own seam.

**Off, or not fetched yet, means absent.** A consumer handed ``None`` instead
of a :class:`LiveCatalogue` produces exactly what it produced before 7.84.0.
"""

from collections.abc import Callable
from dataclasses import dataclass

from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.core.model_ids import ResolutionTier

#: The badge every value this rung states carries on the Models page.
LIVE_SOURCE = "openrouter_live"
LIVE_SOURCE_LABEL = "OpenRouter live"

#: How the asking id met an OpenRouter id, in the cross-provider vote's own
#: words (``core/model_ids.candidate_ladder``): the id as written, its pricing
#: tag stripped, the bare model with its tag, the bare model.
LIVE_MATCH_EXACT = "exact id"
LIVE_MATCH_TAG_STRIPPED = "tag stripped"
LIVE_MATCH_BARE_TAGGED = "bare model + tag"
LIVE_MATCH_BARE = "bare model"

#: What a value from this rung is good for, said once beside every one of them.
LIVE_NOTE = (
    "From OpenRouter's own live model list. Shown here and in agent "
    "catalogues only: routing, output caps and reasoning never read it."
)


@dataclass(frozen=True, slots=True)
class LiveModel:
    """What OpenRouter's live list states about one model, as one rung.

    Every field is ``None`` when the list did not state it. When the asking
    id met several OpenRouter rows on its first matching key, a field holds a
    value only where every one of those rows stated the same value
    (``slugs`` names them all); no quorum applies, because there is one
    source.

    ``own_list`` is set when the asking provider IS OpenRouter: its own list
    already answers every existing field at rung 1, so the rung feeds none of
    them (:attr:`feeds_ladder`), and only the new display facts are read.
    """

    slugs: tuple[str, ...]
    match: str
    own_list: bool = False
    modalities: DeclaredModalities | None = None
    supports_vision: bool | None = None
    can_reason: bool | None = None
    supports_tool_calls: bool | None = None
    context_length: int | None = None
    max_output_tokens: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    cache_read_price: float | None = None
    cache_write_price: float | None = None
    reasoning_price: float | None = None
    description: str | None = None
    knowledge_cutoff: str | None = None
    #: ``YYYY-MM-DD``: the day OpenRouter listed it (its ``created``). Never a
    #: release date.
    listed_at: str | None = None

    @property
    def feeds_ladder(self) -> bool:
        """Whether this answer may fill or replace an existing field."""

        return not self.own_list

    @property
    def tier_label(self) -> str:
        """``"OpenRouter live, exact id"``: the badge's rung line."""

        return f"{LIVE_SOURCE_LABEL}, {self.match}"


type LiveLookup = Callable[[str, str], LiveModel | None]


@dataclass(frozen=True, slots=True)
class LiveCatalogue:
    """One bound read of the stored live list.

    Bound once per listing or page build, like models.dev's lookups: the file
    is read and indexed once, and each question after that is a dict lookup.
    ``mark`` is the file's identity, for any cache keyed on what a page was
    computed from; ``fetched_at`` is when the list on disk was downloaded.
    """

    mark: str
    fetched_at: str | None
    rows: int
    lookup: LiveLookup

    def __call__(self, provider_id: str, model_id: str) -> LiveModel | None:
        return self.lookup(provider_id, model_id)


def live_wins_intrinsic(
    existing: object,
    existing_tier: ResolutionTier | int | None,
    live: object,
) -> bool:
    """Whether an intrinsic field's live value is the answer.

    Yes where the live list states one and either nothing above it did, or
    what did is models.dev's OpenRouter copy (tiers 5-6) or the cross-provider
    vote (7-10) -- rungs only a provider with no models.dev bucket ever reaches.
    A provider's own statement (tiers 1-2, or a cached record that carries no
    tier) and a bucket's (3-4) are never replaced.
    """

    if live is None:
        return False
    if existing is None:
        return True
    return existing_tier is not None and int(existing_tier) >= int(
        ResolutionTier.OPENROUTER_EXACT
    )


def live_fills_gap(existing: object, live: object) -> bool:
    """Whether a number's live value is the answer: only where nothing else said."""

    return existing is None and live is not None


__all__ = [
    "LIVE_MATCH_BARE",
    "LIVE_MATCH_BARE_TAGGED",
    "LIVE_MATCH_EXACT",
    "LIVE_MATCH_TAG_STRIPPED",
    "LIVE_NOTE",
    "LIVE_SOURCE",
    "LIVE_SOURCE_LABEL",
    "LiveCatalogue",
    "LiveLookup",
    "LiveModel",
    "live_fills_gap",
    "live_wins_intrinsic",
]
