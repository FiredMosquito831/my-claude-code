"""One normaliser for provider/vendor/model ids and their pricing tags.

A route reaching MCC looks like ``<provider>/<vendor>/<model><tag>``, and the
same id has to be matched against three different tables: a provider's own
``/models`` payload, one models.dev provider bucket, and -- only when
models.dev has no bucket for that provider at all -- every models.dev bucket at
once. Those three lookups previously each carried their own idea of what a
model id is, and the loosest of them carried none, which is why a tagged model
matched almost nothing across providers.

Everything about id shape lives here so there is exactly one answer:

* what the alternative spellings of an id are (:func:`normalize_candidates`),
* which trailing words are pricing/routing tags rather than part of the model
  (:data:`STRIPPABLE_MODEL_ID_TAGS`, :func:`strip_model_id_tag`),
* and in what order a lookup may loosen the id (:func:`candidate_ladder`).
"""

from enum import IntEnum


class ResolutionTier(IntEnum):
    """Where one resolved capability/limit field came from, tightest first.

    A route is ``<provider>/<vendor>/<model><tag>``. Each rung loosens exactly
    one thing relative to the rung above it, and there are only two loosenings,
    applied in a fixed order: **drop the pricing/routing tag before dropping
    the vendor prefix**. Resolution stops at the first rung that answers, and
    the rung is carried back so callers can say how much to trust the number.

    Tiers 1-4 are authoritative: they are this model, on this provider (or on
    the models.dev bucket that describes exactly this provider). Tiers 5-8 are
    approximations assembled from rows in *other* providers' buckets that
    merely share a name, and are subject to a minimum-sample guard before they
    may supply a number or an effort vocabulary.
    """

    PROVIDER_EXACT = 1
    PROVIDER_TAG_STRIPPED = 2
    MODELS_DEV_BUCKET_EXACT = 3
    MODELS_DEV_BUCKET_TAG_STRIPPED = 4
    CROSS_PROVIDER_EXACT = 5
    CROSS_PROVIDER_TAG_STRIPPED = 6
    CROSS_PROVIDER_BARE_TAGGED = 7
    CROSS_PROVIDER_BARE_UNTAGGED = 8
    FALLBACK_DEFAULT = 9

    @property
    def is_approximate(self) -> bool:
        """Whether this rung matched on name alone, across foreign buckets."""
        return CROSS_PROVIDER_TIERS[0] <= self <= CROSS_PROVIDER_TIERS[-1]

    @property
    def is_authoritative(self) -> bool:
        """Whether this rung is this provider's (or its bucket's) own answer."""
        return self <= ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED


CROSS_PROVIDER_TIERS: tuple[ResolutionTier, ...] = (
    ResolutionTier.CROSS_PROVIDER_EXACT,
    ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED,
    ResolutionTier.CROSS_PROVIDER_BARE_TAGGED,
    ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED,
)


# Trailing words that name a *route* to a model rather than the model.
#
# Strictly allow-listed, because the excluded tags ARE the model difference:
# ":thinking" (69 occurrences upstream), numeric budget tags (":32000",
# ":32768", ":8192", ":1024", ":64000") and effort tags (":low", ":medium",
# ":high", ":max") are NEVER stripped -- models.dev ships
# "nano-gpt/claude-opus-4-thinking:32000" and ":32768", and
# "gemini-2.5-flash-preview:thinking" -- so stripping those would be wrong in
# exactly the dimension being resolved.
STRIPPABLE_MODEL_ID_TAGS: frozenset[str] = frozenset(
    {"free", "paid", "nitro", "floor", "online", "extended", "discounted"}
)

# Gateways spell the same tag both ways: OpenRouter-style "tencent/hy3:free"
# and CommandCode-style "minimax/minimax-m3-free" name one routing variant of
# one model. Only these two separators, and only for the allow-listed words --
# a hyphen is otherwise a normal character inside a model name.
_TAG_SEPARATORS: tuple[str, ...] = (":", "-")


def normalize_candidates(model_id: str) -> set[str]:
    """Return the match keys for one model id: the full id and its bare tail."""
    lowered = model_id.strip().lower()
    if not lowered:
        return set()
    candidates = {lowered}
    _prefix, separator, remainder = lowered.partition("/")
    if separator and remainder:
        candidates.add(remainder)
    last_segment = lowered.rsplit("/", 1)[-1]
    if last_segment:
        candidates.add(last_segment)
    return candidates


def strip_model_id_tag(model_id: str) -> str | None:
    """Return ``model_id`` minus a trailing pricing/routing tag, else ``None``.

    ``None`` when the id carries no tag, carries a tag outside the allow-list,
    or is nothing but a tag. The tag is only ever looked for in the *last* path
    segment, so a vendor prefix is left completely alone.
    """
    lowered = model_id.strip().lower()
    if not lowered:
        return None
    prefix, _slash, last_segment = lowered.rpartition("/")
    for separator in _TAG_SEPARATORS:
        head, found, tag = last_segment.rpartition(separator)
        if not found or not head or tag not in STRIPPABLE_MODEL_ID_TAGS:
            continue
        return f"{prefix}/{head}" if _slash else head
    return None


def bare_model_id(model_id: str) -> str:
    """Return just the last path segment: the model without its vendor prefix."""
    return model_id.strip().lower().rsplit("/", 1)[-1]


def candidate_ladder(model_id: str) -> tuple[tuple[ResolutionTier, str], ...]:
    """The cross-provider rungs for ``model_id``, tightest first.

    Exactly two loosenings, in a fixed order -- drop the tag, then drop the
    vendor prefix -- giving tiers 5 through 8. Rungs that would repeat an
    earlier key (an untagged id, or one with no vendor prefix) are omitted
    rather than retried, so a caller can stop at the first hit and report the
    rung honestly.
    """
    full = model_id.strip().lower()
    if not full:
        return ()
    rungs: list[tuple[ResolutionTier, str]] = []
    seen: set[str] = set()

    def add(tier: ResolutionTier, key: str | None) -> None:
        if key and key not in seen:
            seen.add(key)
            rungs.append((tier, key))

    add(ResolutionTier.CROSS_PROVIDER_EXACT, full)
    add(ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED, strip_model_id_tag(full))
    bare = bare_model_id(full)
    add(ResolutionTier.CROSS_PROVIDER_BARE_TAGGED, bare)
    add(ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED, strip_model_id_tag(bare))
    return tuple(rungs)
