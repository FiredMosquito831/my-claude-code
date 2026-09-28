"""What one media request cost, from the same ladder a chat request walks.

``application.cost`` prices tokens. A media answer is also measured in the
units its sources publish prices in -- images, seconds, characters -- so this
module carries those beside the token counters and walks the same rungs in the
same order: the host's own figure, then models.dev, then LiteLLM when the
operator turned it on, then the cross-provider vote, then nothing.

The rules are chat's, unchanged (7.69.0):

* **No price lives here.** Every rate arrives on a :class:`MediaRateCard` that
  a fetcher built from a cached, revalidated source with provenance.
* **Units are the source's own.** A per-image rate multiplies an image count, a
  per-second rate multiplies seconds, a per-character rate multiplies
  characters, a per-token rate multiplies tokens. Nothing is converted into
  another unit: seconds are never turned into tokens, and a per-second rate is
  never turned into a per-minute one.
* **Each unit rate is read only for the operation it measures.** A
  transcription is priced by the seconds of audio it heard; a transcript has no
  seconds, so an entry's *output* per-second rate never applies to one. Speech
  is priced by the characters it was asked to speak, else by the seconds of
  audio it produced; a video by the seconds of video it produced; an image
  request by the images it returned.
* **Unknown is NULL, never $0.** A rung that computes zero has not priced
  anything -- "listed free" is not a source -- and the walk moves on. What no
  rung prices is stored as ``unpriced`` with no amount.
* **One rung prices a request whole.** A card is never patched with another
  card's rate.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from my_claude_code.application.cost import (
    MODE_AUTO,
    MODE_COMPUTED_ONLY,
    MODE_REPORTED_ONLY,
    SOURCE_PROVIDER,
    SOURCE_UNPRICED,
    CostResult,
    RateCard,
    TokenUsage,
    compute_from_rates,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
)

_IMAGE_OPERATIONS = frozenset(
    {MEDIA_OPERATION_IMAGE_GENERATE, MEDIA_OPERATION_IMAGE_EDIT}
)
_TRANSCRIPT_OPERATIONS = frozenset(
    {MEDIA_OPERATION_TRANSCRIBE, MEDIA_OPERATION_TRANSLATE}
)

#: Where a host's usage block itemises audio tokens, in the spellings the
#: OpenAI answers use (a transcription says ``input_token_details``, an image
#: answer ``input_tokens_details``, a chat-shaped one ``prompt_tokens_details``).
_INPUT_DETAIL_KEYS = (
    "input_token_details",
    "input_tokens_details",
    "prompt_tokens_details",
)
_OUTPUT_DETAIL_KEYS = (
    "output_token_details",
    "output_tokens_details",
    "completion_tokens_details",
)
_AUDIO_TOKENS_KEY = "audio_tokens"


@dataclass(frozen=True, slots=True)
class MediaUsage:
    """What one media request measured, in every unit a source may price.

    Each field is ``None`` when nothing measured it. The token counters are
    the host's own; ``input_audio_tokens`` / ``output_audio_tokens`` are the
    part of them the host itemised as audio, never an estimate.
    """

    operation: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    input_audio_tokens: int | None = None
    output_audio_tokens: int | None = None
    images_out: int | None = None
    input_chars: int | None = None
    input_audio_seconds: float | None = None
    output_audio_seconds: float | None = None
    output_video_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class MediaRateCard:
    """One source's rates for one media model.

    ``tokens`` is the chat rate card (USD per single token, normalised by its
    fetcher). The audio-token rates are USD per single audio token and refine
    only the audio part a host itemised: a card without one prices those
    tokens at its own input/output rate, exactly as a card without a reasoning
    rate prices reasoning as output. The unit rates are USD per image, per
    character, and per second -- as the source publishes them.
    """

    source: str
    tokens: RateCard | None = None
    input_audio_price: float | None = None
    output_audio_price: float | None = None
    per_image: float | None = None
    per_character: float | None = None
    per_input_second: float | None = None
    per_output_second: float | None = None
    tier_label: str | None = None

    @property
    def is_empty(self) -> bool:
        """Whether the card could price nothing at all."""
        if self.tokens is not None and not self.tokens.is_empty:
            return False
        return all(
            rate is None
            for rate in (
                self.per_image,
                self.per_character,
                self.per_input_second,
                self.per_output_second,
            )
        )


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _detail_count(usage: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        details = usage.get(key)
        if isinstance(details, Mapping):
            found = _count(details.get(_AUDIO_TOKENS_KEY))
            if found is not None:
                return found
    return None


def reported_audio_tokens(
    usage: Mapping[str, Any] | None,
) -> tuple[int | None, int | None]:
    """``(audio tokens in, audio tokens out)`` a usage block itemised, or None each."""
    if usage is None:
        return None, None
    return (
        _detail_count(usage, _INPUT_DETAIL_KEYS),
        _detail_count(usage, _OUTPUT_DETAIL_KEYS),
    )


def _times(quantity: float | None, rate: float | None) -> float | None:
    """``quantity`` units at ``rate`` per unit; None unless both are known."""
    if quantity is None or rate is None or not quantity > 0:
        return None
    return quantity * rate


def unit_cost(usage: MediaUsage, card: MediaRateCard) -> float | None:
    """The operation's own unit at the card's rate for that unit, or None."""
    operation = usage.operation
    if operation in _IMAGE_OPERATIONS:
        return _times(usage.images_out, card.per_image)
    if operation == MEDIA_OPERATION_SPEECH:
        by_character = _times(usage.input_chars, card.per_character)
        if by_character is not None:
            return by_character
        return _times(usage.output_audio_seconds, card.per_output_second)
    if operation in _TRANSCRIPT_OPERATIONS:
        return _times(usage.input_audio_seconds, card.per_input_second)
    if operation == MEDIA_OPERATION_VIDEO_CREATE:
        return _times(usage.output_video_seconds, card.per_output_second)
    return None


def token_cost(usage: MediaUsage, card: MediaRateCard) -> float | None:
    """The host's token counts at the card's token rates, or None.

    Audio tokens the host itemised are taken out of the totals and priced at
    the card's audio rate for that direction, when it states one; otherwise
    they stay in the totals and price at the card's own rate.
    """
    rates = card.tokens
    if rates is None:
        return None
    tokens_in = usage.tokens_in or 0
    tokens_out = usage.tokens_out or 0
    if not tokens_in and not tokens_out:
        return None
    in_rate = card.input_audio_price
    out_rate = card.output_audio_price
    audio_in = (
        min(usage.input_audio_tokens or 0, tokens_in) if in_rate is not None else 0
    )
    audio_out = (
        min(usage.output_audio_tokens or 0, tokens_out) if out_rate is not None else 0
    )
    base = compute_from_rates(
        TokenUsage(tokens_in=tokens_in - audio_in, tokens_out=tokens_out - audio_out),
        rates,
    )
    if base is None:
        return None
    total = base
    if in_rate is not None:
        total += audio_in * in_rate
    if out_rate is not None:
        total += audio_out * out_rate
    return total


def card_cost(usage: MediaUsage, card: MediaRateCard) -> float | None:
    """What one card prices this request at, or None when it cannot.

    The operation's own unit first, then tokens: a unit rate is the price the
    source publishes for exactly what the operation produced, while a token
    rate on a multimodal entry is usually its text rate. A zero from either is
    not a price ("listed free" is not a source), so it falls through.
    """
    for amount in (unit_cost(usage, card), token_cost(usage, card)):
        if amount is not None and amount > 0:
            return amount
    return None


def resolve_media_cost(
    *,
    reported_usd: float | None,
    usage: MediaUsage,
    cards: Sequence[MediaRateCard],
    mode: str = MODE_AUTO,
) -> CostResult:
    """Walk the ladder once: reported, then each card in order, then unpriced.

    ``cards`` are the computed rungs in ladder order -- models.dev, LiteLLM
    (only when the operator turned it on), the cross-provider vote. The mode is
    chat's: ``reported_only`` stops after the host, ``computed_only`` skips
    it. What nothing prices comes back as ``unpriced`` with no amount.
    """
    if mode != MODE_COMPUTED_ONLY and reported_usd is not None:
        return CostResult(cost_usd=reported_usd, cost_source=SOURCE_PROVIDER)
    if mode != MODE_REPORTED_ONLY:
        for card in cards:
            if card.is_empty:
                continue
            amount = card_cost(usage, card)
            if amount is not None:
                return CostResult(
                    cost_usd=amount,
                    cost_source=card.source,
                    tier_label=card.tier_label,
                )
    return CostResult(cost_source=SOURCE_UNPRICED)


__all__ = [
    "MediaRateCard",
    "MediaUsage",
    "card_cost",
    "reported_audio_tokens",
    "resolve_media_cost",
    "token_cost",
    "unit_cost",
]
